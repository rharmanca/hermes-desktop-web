#!/usr/bin/env python3
"""Hermes Desktop Web - preview relay.

Gives the browser-hosted renderer a real browser engine for its preview pane
(the Electron app's <webview>). A dedicated headless Chromium runs on the
host; this relay exposes one WebSocket per pane that streams the tab as
JPEG frames (CDP Page.screencast) and accepts the same input/navigation
calls the Electron <webview> offers. The web bridge's <webview> custom
element (deploy/robweb/web-preview.js) is the other half.

Listens only on loopback + the docker bridge address so the desktop-web
container (nginx, behind the Hermes login via auth_request) is the only
reachable path. No HTTP routes besides /healthz and /ws.
"""

import asyncio
import base64
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import websockets
from websockets.asyncio.server import serve

LOG = logging.getLogger("preview-relay")

CHROMIUM = os.environ.get("PREVIEW_CHROMIUM", "/usr/lib/chromium/chromium")
PROFILE_DIR = Path(os.environ.get(
    "PREVIEW_PROFILE_DIR",
    Path.home() / ".hermes/profiles/hermes-admin/browser-preview-profile"))
CDP_PORT = int(os.environ.get("PREVIEW_CDP_PORT", "9340"))
BIND_HOSTS = [h for h in os.environ.get("PREVIEW_BIND", "127.0.0.1,172.17.0.1").split(",") if h]
BIND_PORT = int(os.environ.get("PREVIEW_PORT", "18091"))
MAX_PANES = int(os.environ.get("PREVIEW_MAX_PANES", "8"))
JPEG_QUALITY = int(os.environ.get("PREVIEW_JPEG_QUALITY", "70"))
MODIFIER_BITS = {"alt": 1, "control": 2, "ctrl": 2, "meta": 4, "shift": 8}


# ---------------------------------------------------------------- chromium --
class Chromium:
    def __init__(self):
        self.proc = None

    def start(self):
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(PROFILE_DIR, 0o700)
        args = [
            CHROMIUM, "--headless=new", "--disable-gpu", "--disable-dev-shm-usage",
            "--no-first-run", "--no-default-browser-check", "--disable-default-apps",
            "--disable-extensions", "--hide-scrollbars=false", "--mute-audio",
            f"--remote-debugging-port={CDP_PORT}", "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={PROFILE_DIR}", "--window-size=1280,800", "about:blank",
        ]
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True)
        for _ in range(100):
            if self.proc.poll() is not None:
                raise RuntimeError(f"chromium exited early ({self.proc.returncode})")
            try:
                self.version()
                LOG.info("chromium ready on 127.0.0.1:%d (profile %s)", CDP_PORT, PROFILE_DIR)
                return
            except Exception:
                time.sleep(0.2)
        raise RuntimeError("chromium did not come up")

    @staticmethod
    def version():
        with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=2) as r:
            return json.load(r)

    @staticmethod
    def new_target():
        req = urllib.request.Request(f"http://127.0.0.1:{CDP_PORT}/json/new?about:blank", method="PUT")
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r)

    @staticmethod
    def close_target(target_id):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/close/{target_id}", timeout=5).read()
        except Exception:
            pass

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


CHROME = Chromium()


# --------------------------------------------------------------------- pane --
class Pane:
    """One renderer <webview> <-> one Chromium tab."""

    def __init__(self, client):
        self.client = client
        self.target = None
        self.cdp = None
        self.seq = 0
        self.pending = {}
        self.width, self.height = 1024, 700
        self.scale = 1.0
        self.main_frame = None
        self.closed = False

    async def open(self):
        self.target = CHROME.new_target()
        self.cdp = await websockets.connect(self.target["webSocketDebuggerUrl"], max_size=32 * 1024 * 1024)
        # The pump must never await a CDP reply itself (it is the only reader),
        # so events go through a queue to a separate dispatcher task.
        self._events = asyncio.Queue()
        self._tasks = [asyncio.create_task(self._pump()), asyncio.create_task(self._dispatch())]
        await self.call("Page.enable")
        await self.call("Runtime.enable")
        await self.call("Network.enable")
        await self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        tree = await self.call("Page.getFrameTree")
        self.main_frame = tree["frameTree"]["frame"]["id"]
        await self.resize(self.width, self.height, self.scale)
        await self.start_cast()

    async def start_cast(self):
        await self.call("Page.startScreencast", format="jpeg", quality=JPEG_QUALITY,
                        maxWidth=int(self.width * self.scale), maxHeight=int(self.height * self.scale),
                        everyNthFrame=1)

    async def resize(self, w, h, scale):
        self.width, self.height, self.scale = max(100, int(w)), max(100, int(h)), min(2.0, max(1.0, float(scale)))
        await self.call("Emulation.setDeviceMetricsOverride", width=self.width, height=self.height,
                        deviceScaleFactor=self.scale, mobile=False)

    async def call(self, method, **params):
        self.seq += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[self.seq] = fut
        await self.cdp.send(json.dumps({"id": self.seq, "method": method, "params": params}))
        return await asyncio.wait_for(fut, 30)

    async def _pump(self):
        try:
            async for raw in self.cdp:
                msg = json.loads(raw)
                if "id" in msg:
                    fut = self.pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        if "error" in msg:
                            fut.set_exception(RuntimeError(msg["error"].get("message") or "cdp error"))
                        else:
                            fut.set_result(msg.get("result", {}))
                else:
                    self._events.put_nowait((msg.get("method"), msg.get("params", {})))
        except Exception as exc:  # tab died / socket closed
            if not self.closed:
                LOG.info("cdp pump ended: %s", exc)
                await self.emit("did-fail-load", errorCode=-2, errorDescription="preview tab closed",
                                validatedURL="", isMainFrame=True)
        finally:
            self._events.put_nowait(None)

    async def _dispatch(self):
        while True:
            item = await self._events.get()
            if item is None:
                return
            try:
                await self._event(*item)
            except Exception as exc:
                if not self.closed:
                    LOG.info("event %s failed: %s", item[0], exc)

    async def emit(self, event, **detail):
        if self.closed:
            return
        try:
            await self.client.send(json.dumps({"event": event, **detail}))
        except Exception:
            pass

    async def _event(self, method, p):
        if method == "Page.screencastFrame":
            try:
                await self.client.send(json.dumps({"frame": p["data"], "meta": p["metadata"]}))
            finally:
                try:
                    await self.call("Page.screencastFrameAck", sessionId=p["sessionId"])
                except Exception:
                    pass
        elif method == "Page.frameStartedLoading" and p.get("frameId") == self.main_frame:
            await self.emit("did-start-loading")
        elif method == "Page.frameNavigated" and p["frame"].get("id") == self.main_frame:
            await self.emit("did-navigate", url=p["frame"].get("url", ""))
        elif method == "Page.navigatedWithinDocument" and p.get("frameId") == self.main_frame:
            await self.emit("did-navigate-in-page", url=p.get("url", ""), isMainFrame=True)
        elif method == "Page.domContentEventFired":
            await self.emit("dom-ready")
        elif method == "Page.loadEventFired":
            await self.emit("did-finish-load")
            await self.emit("did-stop-loading")
            await self._title()
            await self._history()
        elif method == "Page.frameStoppedLoading" and p.get("frameId") == self.main_frame:
            await self.emit("did-stop-loading")
            await self._title()
            await self._history()
        elif method == "Network.loadingFailed" and p.get("type") == "Document":
            await self.emit("did-fail-load", errorCode=-105 if "NAME_NOT_RESOLVED" in p.get("errorText", "") else -2,
                            errorDescription=p.get("errorText", "load failed"), validatedURL="", isMainFrame=True)
            await self.emit("did-stop-loading")
        elif method == "Runtime.consoleAPICalled":
            level = {"error": 3, "warning": 2, "info": 1}.get(p.get("type"), 0)
            text = " ".join(str(a.get("value", a.get("description", ""))) for a in p.get("args", []))
            await self.emit("console-message", level=level, message=text, line=0, sourceId="")
        elif method == "Page.javascriptDialogOpening":
            # Headless has no user to answer; dismiss so the tab never wedges.
            await self.call("Page.handleJavaScriptDialog", accept=p.get("type") != "beforeunload")

    async def _title(self):
        try:
            r = await self.call("Runtime.evaluate", expression="document.title", returnByValue=True)
            await self.emit("page-title-updated", title=r.get("result", {}).get("value", ""))
        except Exception:
            pass

    async def _history(self):
        try:
            h = await self.call("Page.getNavigationHistory")
            i, n = h["currentIndex"], len(h["entries"])
            await self.emit("history", canGoBack=i > 0, canGoForward=i < n - 1,
                            url=h["entries"][i]["url"] if n else "")
        except Exception:
            pass

    async def history_step(self, delta):
        h = await self.call("Page.getNavigationHistory")
        i = h["currentIndex"] + delta
        if 0 <= i < len(h["entries"]):
            await self.call("Page.navigateToHistoryEntry", entryId=h["entries"][i]["id"])

    async def handle(self, m):
        op = m.get("op")
        if op == "navigate":
            url = str(m.get("url", "")).strip()
            if not url.lower().startswith(("http://", "https://", "about:blank")):
                raise ValueError("only http(s) URLs may be previewed")
            await self.call("Page.navigate", url=url)
        elif op == "resize":
            await self.resize(m["width"], m["height"], m.get("scale", 1))
            await self.call("Page.stopScreencast")
            await self.start_cast()
        elif op == "back":
            await self.history_step(-1)
        elif op == "forward":
            await self.history_step(1)
        elif op == "reload":
            await self.call("Page.reload", ignoreCache=bool(m.get("ignoreCache")))
        elif op == "eval":
            r = await self.call("Runtime.evaluate", expression=str(m.get("code", "")), awaitPromise=True,
                                returnByValue=True, userGesture=True)
            if "exceptionDetails" in r:
                ex = r["exceptionDetails"]
                raise RuntimeError(ex.get("exception", {}).get("description") or ex.get("text", "script error"))
            return r.get("result", {}).get("value")
        elif op == "input":
            await self.input(m["event"])
        elif op == "focus":
            await self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        elif op == "state":
            await self._title()
            await self._history()
        else:
            raise ValueError(f"unknown op {op!r}")
        return None

    async def input(self, e):
        t = e.get("type")
        mods = sum(MODIFIER_BITS.get(str(x).lower(), 0) for x in e.get("modifiers", []) or [])
        if t in ("mouseDown", "mouseUp", "mouseMove", "mouseWheel"):
            kind = {"mouseDown": "mousePressed", "mouseUp": "mouseReleased", "mouseMove": "mouseMoved",
                    "mouseWheel": "mouseWheel"}[t]
            params = {"type": kind, "x": float(e.get("x", 0)), "y": float(e.get("y", 0)), "modifiers": mods}
            if t in ("mouseDown", "mouseUp"):
                params.update(button=e.get("button", "left"), clickCount=int(e.get("clickCount", 1)))
            elif t == "mouseMove":
                params.update(button=e.get("button", "none"))
            else:
                params.update(deltaX=float(e.get("deltaX", 0)), deltaY=float(e.get("deltaY", 0)))
            await self.call("Input.dispatchMouseEvent", **params)
        elif t in ("keyDown", "keyUp", "char"):
            params = {"type": {"keyDown": "keyDown", "keyUp": "keyUp", "char": "char"}[t], "modifiers": mods}
            if t == "char":
                params["text"] = str(e.get("keyCode", ""))[:4]
                params["type"] = "char"
            else:
                params.update(key=e.get("key", e.get("keyCode", "")), code=e.get("code", ""),
                              windowsVirtualKeyCode=int(e.get("vk", 0)) or None,
                              nativeVirtualKeyCode=int(e.get("vk", 0)) or None)
                if t == "keyDown" and e.get("text"):
                    params["text"] = str(e["text"])[:4]
                    params["type"] = "keyDown"
                params = {k: v for k, v in params.items() if v is not None}
            await self.call("Input.dispatchKeyEvent", **params)
        else:
            raise ValueError(f"unknown input {t!r}")

    async def close(self):
        self.closed = True
        try:
            if self.cdp:
                await self.cdp.close()
            for t in getattr(self, "_tasks", []):
                t.cancel()
        finally:
            if self.target:
                CHROME.close_target(self.target["id"])


# ------------------------------------------------------------------- server --
PANES = set()


async def ws_handler(ws):
    path = ws.request.path if hasattr(ws, "request") else ""
    if not path.startswith("/ws"):
        await ws.close(code=1008, reason="not found")
        return
    if len(PANES) >= MAX_PANES:
        await ws.close(code=1013, reason="too many preview panes")
        return
    pane = Pane(ws)
    PANES.add(pane)
    LOG.info("pane open (%d active) from %s", len(PANES), ws.remote_address)
    try:
        await pane.open()
        await ws.send(json.dumps({"event": "ready", "webContentsId": CDP_PORT * 1000 + len(PANES)}))
        async for raw in ws:
            m = json.loads(raw)
            rid = m.get("id")
            try:
                result = await pane.handle(m)
                if rid is not None:
                    await ws.send(json.dumps({"id": rid, "result": result}))
            except Exception as exc:
                if rid is not None:
                    await ws.send(json.dumps({"id": rid, "error": str(exc) or exc.__class__.__name__}))
                else:
                    LOG.info("op failed: %s", exc)
    except websockets.ConnectionClosed:
        pass
    except Exception as exc:
        LOG.warning("pane error: %s", exc)
    finally:
        PANES.discard(pane)
        await pane.close()
        LOG.info("pane closed (%d active)", len(PANES))


async def process_request(connection, request):
    if request.path == "/healthz":
        try:
            v = CHROME.version()
            body = json.dumps({"ok": True, "browser": v.get("Browser"), "panes": len(PANES)}).encode()
            return connection.respond(200, body.decode() + "\n")
        except Exception as exc:
            return connection.respond(503, json.dumps({"ok": False, "error": str(exc)}) + "\n")
    if not request.path.startswith("/ws"):
        return connection.respond(404, "not found\n")
    return None


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    CHROME.start()
    stop = asyncio.get_running_loop().create_future()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, lambda: not stop.done() and stop.set_result(None))
    # Explicit per-address binds: loopback (tests) + docker bridge (nginx in the container).
    async with serve(ws_handler, host=BIND_HOSTS, port=BIND_PORT, process_request=process_request,
                     max_size=4 * 1024 * 1024, ping_interval=20, ping_timeout=20):
        LOG.info("preview relay listening on %s:%d", ",".join(BIND_HOSTS), BIND_PORT)
        await stop
    CHROME.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        CHROME.stop()
