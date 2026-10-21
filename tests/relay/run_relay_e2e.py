#!/usr/bin/env python3
"""End-to-end test of the browser-pane relay with a REAL headless Chromium.
Starts its own throwaway relay on spare ports (never touches the live one),
then tries to reach this machine's private services from inside the preview.
Needs: chromium, python websockets, internet access (for the public-page checks).
Run: python3 tests/relay/run_relay_e2e.py"""
import asyncio, json, os, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WS_PORT, CDP_PORT, EGRESS = 18191, 9440, 18192
res = []
def check(name, ok, note=""): res.append((name, bool(ok), "" if ok else note))

async def main():
    import websockets
    tmp = tempfile.mkdtemp(prefix="relay-e2e-")
    env = dict(os.environ, PYTHONUNBUFFERED="1", PREVIEW_PORT=str(WS_PORT), PREVIEW_CDP_PORT=str(CDP_PORT), PREVIEW_EGRESS_PORT=str(EGRESS),
               PREVIEW_BIND="127.0.0.1", PREVIEW_PROFILE_BASE=tmp, PREVIEW_MAX_BROWSERS="1", PREVIEW_MAX_PANES="2")
    proc = subprocess.Popen([sys.executable, "relay.py"], cwd=ROOT / "deploy/robweb/preview-relay", env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(50):
            try: urllib.request.urlopen(f"http://127.0.0.1:{WS_PORT}/healthz", timeout=1); break
            except Exception: time.sleep(0.2)
        async with websockets.connect(f"ws://127.0.0.1:{WS_PORT}/ws?profile=e2e", max_size=2**24) as ws:
            n = 0
            async def call(**m):
                nonlocal n; n += 1; m["id"] = n
                await ws.send(json.dumps(m))
                while True:
                    r = json.loads(await asyncio.wait_for(ws.recv(), 40))
                    if r.get("id") == n: return r
            async def where():
                await asyncio.sleep(3)
                return (await call(op="eval", code="location.href + ' | ' + (document.body?document.body.innerText:'').slice(0,200)")).get("result") or ""

            private = [f"http://127.0.0.1:{WS_PORT}/healthz", f"http://127.0.0.1.nip.io:{WS_PORT}/healthz",
                       "http://localhost:9119/api/status", "http://[::1]:9119/", "http://2130706433:9119/",
                       "http://169.254.169.254/latest/meta-data/", "http://172.17.0.1:18090/"]
            for url in private:
                r = await call(op="navigate", url=url)
                check(f"navigate refused: {url}", "error" in r, str(r)[:120])

            # A public page loads normally (through the proxy).
            r = await call(op="navigate", url="https://example.com/")
            page = await where()
            check("public page loads (example.com)", "error" not in r and page.startswith("https://example.com"), page[:120])

            # From inside that public page, JS tries private targets: all must fail.
            probe = """(async () => { const out = {};
              for (const u of %s) { try { const r = await fetch(u, {mode:'no-cors'}); out[u] = 'REACHED ' + r.type; }
                catch (e) { out[u] = 'blocked'; } } return JSON.stringify(out); })()""" % json.dumps(
                [f"http://127.0.0.1.nip.io:{WS_PORT}/healthz", f"http://127.0.0.1:{WS_PORT}/healthz", "http://localhost:9119/"])
            out = json.loads((await call(op="eval", code=probe)).get("result") or "{}")
            for u, v in out.items():
                check(f"page JS cannot reach {u}", v == "blocked", v)

            # Redirect from a public site to a private one is stopped too.
            r = await call(op="navigate", url=f"https://httpbingo.org/redirect-to?url=http://127.0.0.1.nip.io:{WS_PORT}/healthz")
            page = await where()
            check("public→private redirect does not land on private data", '"panes"' not in page, page[:120])

            # A popup opened by the page goes through the same proxy.
            await call(op="navigate", url="https://example.com/")
            await where()
            await call(op="eval", code=f"window.open('http://127.0.0.1.nip.io:{WS_PORT}/healthz'); 1")
            await asyncio.sleep(3)
            tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=3))
            leaked = []
            for t in tabs:
                if "nip.io" in t.get("url", "") and t.get("webSocketDebuggerUrl"):
                    async with websockets.connect(t["webSocketDebuggerUrl"], max_size=2**24) as c:
                        await c.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": "document.body.innerText", "returnByValue": True}}))
                        txt = json.loads(await c.recv()).get("result", {}).get("result", {}).get("value", "")
                        if '"panes"' in (txt or ""): leaked.append(txt[:80])
            check("popup cannot read private service", not leaked, str(leaked))
    finally:
        proc.terminate(); t0 = time.time()
        try:
            log = proc.communicate(timeout=30)[0]
            check("relay shuts down within 10s of SIGTERM", time.time() - t0 < 10, f"{time.time()-t0:.1f}s")
        except Exception: proc.kill(); log = ""
        left = subprocess.run(["pgrep", "-f", f"user-data-dir={tmp}"], capture_output=True, text=True).stdout.split()
        check("no Chromium left running after shutdown", not left, f"{len(left)} processes")
        check("relay logged egress blocks", "egress blocked" in log, "no 'egress blocked' lines in relay log")

asyncio.run(main())
for n, ok, note in res: print(("PASS  " if ok else "FAIL  ") + n + (f"  — {note}" if note else ""))
f = sum(not r[1] for r in res)
print(f"---- {len(res) - f} passed, {f} failed")
sys.exit(1 if f else 0)
