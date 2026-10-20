"""web-terminal: gateway-side shell PTY for the Hermes Desktop web build.

Exposes one WebSocket — ``/api/plugins/web-terminal/terminal`` — that spawns
the gateway's login shell behind a PTY (the same ``PtyBridge`` the dashboard's
own ``/api/pty`` uses) and pumps raw bytes to/from an xterm.js client. This is
the web-mode equivalent of the desktop app's SSH-terminal path (see
apps/desktop/electron/ssh-connection.ts NOTE(remote-terminal)): the terminal
lives where Hermes lives.

Auth: the dashboard's canonical WS gate (``?ticket=`` / ``?token=``), the same
one ``/api/pty`` and the kanban event stream use — no new credential concept.
Resize rides the dashboard's existing escape: ``ESC[RESIZE:cols;rows]``.

Hardening (robweb build):

* **Fail closed.** The plugin rides two private Hermes APIs
  (``web_server_chat._ws_auth_reason`` / ``_ws_request_is_allowed`` and
  ``PtyBridge``). They are re-resolved on every upgrade; if either is missing
  or has changed shape the upgrade is refused *before* ``accept()`` — a Hermes
  update can only ever turn the terminal off, never open it up. Ordinary chat
  is unaffected either way.
* **Enabled-list check on the WS leg.** The dashboard's ``plugins.enabled``
  gate is HTTP middleware, which never runs for WebSocket upgrades; the handler
  repeats it so a plugin disabled while the server is up stops serving shells.
* **Owner only.** A dashboard login proves *a* user; a shell on the host needs
  *the* operator. The server-stamped ``provider:user_id`` must be listed in
  ``plugins.entries.web-terminal.settings.allowed_users`` — unset means nobody.
* **Two shells at a time**, **20 minutes without keyboard input** closes a
  shell (resize frames are layout, not typing, and do not reset that clock),
  and the shell inherits the dashboard environment **minus Hermes-managed
  secrets** (the same scrub the agent's own background commands get).
* **Bounded input.** The dial-time size is clamped to the core resize ceiling
  (``PtyBridge.spawn`` forwards raw values to ptyprocess, where anything above
  65535 raises inside the *forked child*); one input frame is at most 64 KiB and
  a client may send at most 1 MiB per 10 s. The dashboard-wide WebSocket limit
  is the 384 MiB attachment ceiling — far too large for a shell.
* **No ``?profile=`` routing.** The shell always runs in the dashboard's own
  home; a dial that names a profile is refused before anything is spawned.
* **Audit log** (``$HERMES_HOME/logs/web-terminal-audit.log``, JSON lines):
  open / close / deny / error with peer, identity, size, pid, duration and byte
  counts — never command text or terminal output.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import importlib
import itertools
import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

router = APIRouter()
# Same logger as the dashboard's own /api/pty so refusals land in the service journal.
_log = logging.getLogger("hermes_cli.web_server")

PLUGIN_NAME = "web-terminal"
PLUGIN_VERSION = "0.2.0"

MAX_TERMINALS = 2
IDLE_TIMEOUT_S = 20 * 60  # no keyboard input for this long -> the shell is closed (resize is not input)
# Dial-time size ceiling = hermes_cli.pty_bridge's resize clamp (_MAX_COLS / _MAX_ROWS), so the
# first layout and every later resize agree. spawn() itself does not clamp: a value > 65535
# reaches struct.pack in the forked child and the exception escapes there, not in this handler.
MAX_COLS = 2000
MAX_ROWS = 1000
# Terminal input is keystrokes or a paste. uvicorn's ws_max_size is the dashboard-wide
# attachment ceiling (384 MiB) and cannot be lowered per route, so bound it here.
MAX_INPUT_FRAME_BYTES = 64 * 1024  # ponytail: one frame; a paste larger than this closes the shell
INPUT_WINDOW_S = 10.0
MAX_INPUT_BYTES_PER_WINDOW = 1024 * 1024
_READ_TIMEOUT_S = 0.2
_IDLE_BACKOFF_S = 0.05  # quiet terminal: yield instead of re-polling at once (mirrors core)

# Same resize escape the dashboard's xterm.js sends (web_server_chat._RESIZE_RE).
_RESIZE_RE = re.compile(rb"\x1b\[RESIZE:(\d+);(\d+)\]")

# Close codes mirror hermes_cli/web_routers/chat_ws.py::_ws_gate so logs and the
# browser banner agree on the cause. Auth/origin/disabled refusals happen before
# accept() (the browser sees a 403 handshake); the rest are post-accept closes.
CLOSE_AUTH = 4401        # bad / missing credential, or the core auth API is unavailable
CLOSE_ORIGIN = 4403      # host / origin / peer not allowed
CLOSE_FORBIDDEN = 4403   # authenticated, but not in allowed_users (same code as /api/pty "not allowed")
CLOSE_DISABLED = 4404    # plugin not (or no longer) in plugins.enabled
CLOSE_IDLE = 4409        # closed after IDLE_TIMEOUT_S without keyboard input
CLOSE_EXITED = 4410      # shell exited (mirrors /api/pty)
CLOSE_BUSY = 1013        # terminal limit reached / PTY input stalled
CLOSE_ERROR = 1011       # shell could not start
CLOSE_TOO_BIG = 1009     # one input frame over MAX_INPUT_FRAME_BYTES
CLOSE_POLICY = 1008      # more than MAX_INPUT_BYTES_PER_WINDOW within INPUT_WINDOW_S

_CORE_MODULE = "hermes_cli.web_server_chat"
_BRIDGE_REQUIRED = ("spawn", "is_available", "read", "write", "resize", "close", "is_alive")

# Live terminals: slot key -> bookkeeping. One event loop, so no lock: the
# check-then-reserve in ``terminal_ws`` has no await between the two steps.
_ACTIVE: dict[int, dict[str, Any]] = {}
_SEQ = itertools.count(1)


# --- Core contract -------------------------------------------------------------

def _core() -> Optional[Any]:
    """The dashboard's chat/PTY module, or ``None`` when it no longer exposes the
    private API this plugin depends on.

    Resolved on every call, never cached, so a failure cannot be masked by an
    earlier success. ``None`` means *refuse* — the terminal is the one feature
    that must not degrade to "open" when an upstream refactor moves the gate.
    """
    try:
        mod = importlib.import_module(_CORE_MODULE)
    except Exception:
        return None
    for name in ("_ws_auth_reason", "_ws_request_is_allowed"):
        if not callable(getattr(mod, name, None)):
            return None
    bridge = getattr(mod, "PtyBridge", None)
    if bridge is None or any(not callable(getattr(bridge, n, None)) for n in _BRIDGE_REQUIRED):
        return None
    if not isinstance(getattr(mod, "PtyUnavailableError", None), type):
        return None
    return mod


def _plugin_enabled() -> bool:
    """Mirror ``web_server._plugin_api_runtime_gate`` for the WebSocket leg (HTTP
    middleware does not run for WS upgrades): the plugin must still be in
    ``plugins.enabled`` and not in ``plugins.disabled``. Any failure reads as disabled.
    """
    try:
        from hermes_cli.plugins_cmd import _get_disabled_set, _get_enabled_set
        enabled, disabled = _get_enabled_set(), _get_disabled_set()
    except Exception:
        return False
    return PLUGIN_NAME in enabled and PLUGIN_NAME not in disabled


def _ws_deny_reason(ws: WebSocket, core: Any) -> tuple[Optional[int], str]:
    """``(close_code, detail)`` when the upgrade must be refused, else ``(None, credential)``.

    Gate order and codes match ``_ws_gate``: credential first, then host/origin/peer.
    An exception inside either core gate is a refusal, not a pass.
    """
    try:
        auth_reason, cred = core._ws_auth_reason(ws)
    except Exception as exc:  # shape drift (e.g. a bool return) or a gate crash
        return CLOSE_AUTH, f"auth gate error: {type(exc).__name__}"
    if auth_reason is not None:
        return CLOSE_AUTH, f"auth: {auth_reason}"
    try:
        if not core._ws_request_is_allowed(ws):
            return CLOSE_ORIGIN, "host/origin/peer not allowed"
    except Exception as exc:
        return CLOSE_ORIGIN, f"origin gate error: {type(exc).__name__}"
    return None, str(cred)


# --- Audit log -----------------------------------------------------------------

_AUDIT_MAX_BYTES = 5 * 1024 * 1024
_audit_lock = threading.Lock()


def _audit_path() -> Path:
    # Dashboard-owned asset: the launch home, not a ``?profile=``-routed one.
    from hermes_constants import get_process_hermes_home
    return Path(get_process_hermes_home()) / "logs" / f"{PLUGIN_NAME}-audit.log"


def _audit(event: str, **fields: Any) -> None:
    """Append one JSON line. Lifecycle metadata only — never command text or
    terminal bytes. Never raises: a broken audit log must not break (or open) the terminal.
    """
    try:
        entry = {"ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                 "event": event, **fields}
        line = (json.dumps(entry, separators=(",", ":"), default=str) + "\n").encode("utf-8")
        path = _audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _audit_lock:
            with contextlib.suppress(OSError):
                if path.stat().st_size + len(line) >= _AUDIT_MAX_BYTES:
                    os.replace(path, path.with_name(path.name + ".1"))
            with open(path, "ab") as f:
                f.write(line)
    except Exception as exc:  # health: allow BLE001 -- audit must never break the terminal
        _log.warning("web-terminal audit write failed: %s", exc)


def _identity(ws: WebSocket) -> dict[str, Any]:
    """The server-stamped identity from the core gate (gated mode), else empty."""
    info = getattr(ws, "_hermes_auth_identity", None)
    if not isinstance(info, dict):
        return {}
    return {"user_id": info.get("user_id"), "provider": info.get("provider")}


def _http_identity(request: Request) -> dict[str, Any]:
    """Identity the dashboard auth middleware stamped on an HTTP request (``request.state.session``)."""
    sess = getattr(request.state, "session", None)
    return {"user_id": getattr(sess, "user_id", None), "provider": getattr(sess, "provider", None)}


def _allowed_users() -> frozenset[str]:
    """``plugins.entries.web-terminal.settings.allowed_users`` as ``provider:user_id`` strings
    (e.g. ``basic:alice``). Unset, malformed or unreadable config -> empty -> nobody: the shell
    stays off until the operator names who may open it. Re-read on every dial, never cached here.
    """
    try:
        from hermes_cli.config import load_config_readonly
        entries = ((load_config_readonly() or {}).get("plugins") or {}).get("entries") or {}
        raw = ((entries.get(PLUGIN_NAME) or {}).get("settings") or {}).get("allowed_users")
    except Exception:
        return frozenset()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return frozenset()
    out: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            provider, _, user = item.strip().partition(":")
            if provider and user:
                out.add(f"{provider}:{user}")
    return frozenset(out)


def _user_allowed(identity: dict[str, Any]) -> bool:
    """True only for a server-stamped ``provider:user_id`` that is in ``allowed_users``.
    No identity (e.g. ``--insecure`` loopback mode, or the internal credential) -> False."""
    provider, user = identity.get("provider"), identity.get("user_id")
    if not (isinstance(provider, str) and provider and isinstance(user, str) and user):
        return False
    return f"{provider}:{user}" in _allowed_users()


# --- Shell ---------------------------------------------------------------------

def _shell_argv() -> list[str]:
    shell = os.environ.get("SHELL") or shutil.which("bash") or shutil.which("sh") or "/bin/sh"
    return [shell, "-l"]


def _shell_env() -> dict[str, str]:
    """The dashboard's environment minus Hermes-managed secrets — the same scrub the
    agent's own background commands get. Raises when the scrub is unavailable: a
    failure must not silently fall back to the unscrubbed inherit."""
    from tools.environments.local import build_subprocess_env
    env = build_subprocess_env(scrub_secrets=True)
    if not env.get("TERM"):
        env["TERM"] = "xterm-256color"
    return env


def _dimension(raw: Any, default: int, maximum: int) -> int:
    """One cols/rows value: non-numeric or < 1 -> ``default``; otherwise clamped to ``maximum``.
    ``int()`` of a digit string past the interpreter's digit limit raises ValueError, so a
    kilobyte of 9s lands on ``default`` instead of escaping the handler."""
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return min(n, maximum) if n >= 1 else default


def _reap(bridge: Any) -> threading.Thread:
    """Close the PTY bridge off the event loop on a plain thread — never an executor
    future. ``bridge.close()`` blocks (SIGHUP → SIGTERM → SIGKILL cadence), so it must
    not run on the loop; but a ``run_in_executor``/``to_thread`` future is cancelled
    along with the awaiting task at shutdown (uvicorn's, or the test client's), and a
    not-yet-started work item is then silently dropped — the shell outlives the socket.
    A daemon thread cannot be cancelled; ``close`` is idempotent so a double reap is safe."""
    def _close() -> None:
        with contextlib.suppress(Exception):
            bridge.close()
    thread = threading.Thread(target=_close, name="web-terminal-reap", daemon=True)
    thread.start()
    return thread


async def _fail(ws: WebSocket, message: str, code: int = CLOSE_ERROR) -> None:
    """Post-accept failure: tell the user why (red line in xterm), then close."""
    with contextlib.suppress(Exception):
        await ws.send_text(f"\r\n\x1b[31m{message}\x1b[0m\r\n")
    with contextlib.suppress(Exception):
        await ws.close(code=code, reason=message[:120])


# --- Routes --------------------------------------------------------------------

@router.get("/health")
async def health():
    core = _core()
    pty_ok = bool(core is not None and core.PtyBridge.is_available())
    return {
        "ok": core is not None and pty_ok,
        "contract_ok": core is not None,
        "pty_available": pty_ok,
        "active": len(_ACTIVE),
        "limit": MAX_TERMINALS,
        "idle_timeout_seconds": IDLE_TIMEOUT_S,
        "allowed_users_configured": bool(_allowed_users()),
        "version": PLUGIN_VERSION,
    }


@router.get("/shell")
async def shell_info(request: Request):
    """Preflight for the web bridge: which shell/cwd a terminal would get. Owner-gated like
    the dial — the host's working directory is not for every dashboard login."""
    if not _user_allowed(_http_identity(request)):
        return JSONResponse({"error": "not allowed"}, status_code=403)
    return {"shell": os.path.basename(_shell_argv()[0]), "cwd": os.getcwd()}


@router.websocket("/terminal")
async def terminal_ws(ws: WebSocket) -> None:
    peer = ws.client.host if ws.client else "?"

    # 1. Private-API contract (fail closed: no core gate -> no terminal, ever).
    core = _core()
    if core is None:
        _audit("deny", reason="core_contract", peer=peer)
        _log.warning("web-terminal refused: dashboard private API unavailable or changed "
                     "(failing closed) peer=%s", peer)
        await ws.close(code=CLOSE_AUTH, reason="terminal unavailable: dashboard API contract")
        return

    # 2. Still enabled? (the HTTP-only runtime gate does not cover WS upgrades)
    if not _plugin_enabled():
        _audit("deny", reason="disabled", peer=peer)
        _log.warning("web-terminal refused: plugin disabled peer=%s", peer)
        await ws.close(code=CLOSE_DISABLED, reason="web-terminal plugin disabled")
        return

    # 3. Credential, then host/origin/peer — the dashboard's own gates.
    code, detail = _ws_deny_reason(ws, core)
    if code is not None:
        _audit("deny", reason=detail, peer=peer)
        _log.warning("web-terminal refused: %s peer=%s", detail, peer)
        await ws.close(code=code, reason=detail[:120])
        return
    cred = detail

    # 3a. No profile routing. The dashboard mounts every plugin route behind a
    #     ``?profile=`` scope dependency (HTTP and WS alike); a hand-crafted
    #     ``?profile=X`` would run this handler — the shell's env scrub AND the
    #     allowed_users lookup below — under profile X's config/secret scope. The
    #     shell belongs to the dashboard's own home only, so refuse first.
    if (ws.query_params.get("profile") or "").strip():
        _audit("deny", reason="profile_param", peer=peer, **_identity(ws))
        _log.warning("web-terminal refused: profile routing not supported peer=%s", peer)
        await ws.close(code=CLOSE_ORIGIN, reason="profile routing not supported")
        return

    # 3b. Owner gate. The credential proved *a* dashboard user; a shell on the host needs
    #     *the* operator. Identity is whatever the core gate stamped — never a client parameter.
    ident = _identity(ws)
    if not _user_allowed(ident):
        _audit("deny", reason="not_allowed", peer=peer, **ident)
        _log.warning("web-terminal refused: user not in allowed_users peer=%s", peer)
        await ws.close(code=CLOSE_FORBIDDEN, reason="not allowed to open a terminal")
        return

    # 4. Concurrency cap. Reserve the slot before the first await so two dials
    #    racing past the check cannot both spawn.
    if len(_ACTIVE) >= MAX_TERMINALS:
        _audit("deny", reason="limit", peer=peer, active=len(_ACTIVE), limit=MAX_TERMINALS,
               **_identity(ws))
        _log.warning("web-terminal refused: limit %d reached peer=%s", MAX_TERMINALS, peer)
        await ws.accept()
        await _fail(ws, f"terminal limit reached ({MAX_TERMINALS} open shells)", code=CLOSE_BUSY)
        return
    key = next(_SEQ)
    slot: dict[str, Any] = {"peer": peer, "opened": time.time(), "pid": None}
    _ACTIVE[key] = slot

    bridge = None
    pump: Optional[asyncio.Task] = None
    loop = asyncio.get_running_loop()
    state = {"last_input": loop.time(), "bytes_in": 0, "bytes_out": 0, "close": "client_disconnect",
             "win_start": loop.time(), "win_bytes": 0}
    try:
        await ws.accept()

        cols = _dimension(ws.query_params.get("cols"), 80, MAX_COLS)
        rows = _dimension(ws.query_params.get("rows"), 24, MAX_ROWS)
        cwd = (ws.query_params.get("cwd") or "").strip() or None
        if cwd is not None and not os.path.isdir(cwd):
            cwd = None  # stale restore cwd: fall back to the server cwd, never fail the dial

        try:
            bridge = core.PtyBridge.spawn(_shell_argv(), cwd=cwd, env=_shell_env(), cols=cols, rows=rows)
        except Exception as exc:  # health: allow BLE001 -- PtyUnavailableError / OSError / scrub failure: close cleanly
            _log.warning("web-terminal spawn failed: %s: %s", type(exc).__name__, exc)
            _audit("error", reason=f"spawn: {type(exc).__name__}", peer=peer, **_identity(ws))
            await _fail(ws, "terminal could not start (see the dashboard log)")
            return
        slot["pid"] = getattr(bridge, "pid", None)
        _audit("open", peer=peer, cred=cred, pid=slot["pid"], cols=cols, rows=rows, cwd=cwd,
               active=len(_ACTIVE), **_identity(ws))
        _log.info("web-terminal opened peer=%s cred=%s pid=%s", peer, cred, slot["pid"])

        async def pump_pty_to_ws() -> None:
            try:
                while True:
                    if loop.time() - state["last_input"] > IDLE_TIMEOUT_S:
                        state["close"] = "idle_timeout"
                        with contextlib.suppress(Exception):
                            await ws.close(code=CLOSE_IDLE,
                                           reason=f"closed after {IDLE_TIMEOUT_S // 60} min without input")
                        return
                    # bridge.read blocks up to the poll timeout in a worker thread; None = EOF.
                    chunk = await asyncio.to_thread(bridge.read, _READ_TIMEOUT_S)
                    if chunk is None:
                        state["close"] = "shell_exited"
                        with contextlib.suppress(Exception):
                            await ws.close(code=CLOSE_EXITED, reason="shell exited")
                        return
                    if not chunk:
                        await asyncio.sleep(_IDLE_BACKOFF_S)
                        continue
                    state["bytes_out"] += len(chunk)
                    try:
                        await ws.send_bytes(chunk)
                    except Exception:
                        # The writer may already have closed the socket for a reason of its
                        # own (flood, oversized frame, stall); keep that reason for the audit.
                        if state["close"] == "client_disconnect":
                            state["close"] = "send_failed"
                        return
            finally:
                # Reap here too (idempotent; plain thread, see _reap) and close the socket
                # so the writer's ws.receive() unparks instead of hanging on a half-open
                # browser socket.
                _reap(bridge)
                with contextlib.suppress(Exception):
                    await ws.close()

        pump = asyncio.create_task(pump_pty_to_ws())

        while True:
            try:
                msg = await ws.receive()
            except RuntimeError:  # receive() after the pump already closed the socket
                break
            if msg.get("type") == "websocket.disconnect":
                break
            raw = msg.get("bytes")
            if raw is None:
                text = msg.get("text")
                raw = text.encode("utf-8") if isinstance(text, str) else b""
            if not raw:
                continue
            if len(raw) > MAX_INPUT_FRAME_BYTES:
                state["close"] = "input_too_large"
                with contextlib.suppress(Exception):
                    await ws.close(code=CLOSE_TOO_BIG, reason="input frame too large")
                break
            now = loop.time()
            if now - state["win_start"] >= INPUT_WINDOW_S:
                state["win_start"], state["win_bytes"] = now, 0
            state["win_bytes"] += len(raw)
            if state["win_bytes"] > MAX_INPUT_BYTES_PER_WINDOW:
                state["close"] = "input_flood"
                with contextlib.suppress(Exception):
                    await ws.close(code=CLOSE_POLICY, reason="input rate limit")
                break
            # Resize escapes ride the input stream; consume them, forward the rest. A
            # resize-only frame is layout, not typing: it must not reset the idle clock.
            match = _RESIZE_RE.search(raw)
            if match:
                new_cols = _dimension(match.group(1), 0, MAX_COLS)
                new_rows = _dimension(match.group(2), 0, MAX_ROWS)
                if new_cols and new_rows:
                    bridge.resize(new_cols, new_rows)
                raw = raw[: match.start()] + raw[match.end():]
                if not raw:
                    continue
            state["last_input"] = now
            state["bytes_in"] += len(raw)
            if not await bridge.write(raw):
                state["close"] = "input_stalled"
                with contextlib.suppress(Exception):
                    await ws.close(code=CLOSE_BUSY, reason="PTY input stalled")
                break
    except WebSocketDisconnect:
        pass
    finally:
        # Cancellation-safe teardown: the handler task is cancelled on server shutdown
        # (and by Starlette's TestClient), and a cancelled task gets CancelledError at
        # its next await. So: every must-happen step runs synchronously FIRST — slot
        # release, audit line, pump cancel, and the reap on a plain thread (never an
        # executor future, which shutdown would discard) — and only then the
        # best-effort await of the pump.
        _ACTIVE.pop(key, None)
        if bridge is not None:
            _audit("close", peer=peer, pid=slot["pid"], reason=state["close"],
                   duration_s=round(time.time() - slot["opened"], 1),
                   bytes_in=state["bytes_in"], bytes_out=state["bytes_out"],
                   active=len(_ACTIVE), **_identity(ws))
            _log.info("web-terminal closed peer=%s pid=%s reason=%s", peer, slot["pid"], state["close"])
        if pump is not None:
            pump.cancel()
        if bridge is not None:
            _reap(bridge)
        if pump is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pump
