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

* **Fail closed on the private API.** The plugin rides two private Hermes APIs
  (``web_server_chat._ws_auth_reason`` / ``_ws_request_is_allowed`` and
  ``PtyBridge``). They are re-resolved on every upgrade and checked for the exact
  shape this plugin was written against (synchronous callables; a 2-tuple from the
  auth gate; exactly ``True`` from the origin gate). Anything else refuses *before*
  ``accept()``. This is a contract check, not a guarantee: a future core change
  that keeps the shape but changes the meaning cannot be detected here.
* **Policy is read only from the launch home, only when readable.** ``plugins.enabled``
  / ``plugins.disabled`` and ``allowed_users`` are read through ``load_config_readonly``;
  a read or parse failure (Hermes then serves a *last-known-good* or default
  ``FailedConfigRead``) and a request scoped to another profile's home both count
  as "unreadable" and deny. A ``?profile=`` request is refused before the handler
  reads any configuration (the dashboard's mount-level profile dependency has already
  entered that profile's scope by then; the handler neither reads nor spawns under it).
* **Owner only.** A dashboard login proves *a* user; a shell on the host needs
  *the* operator. The server-stamped ``provider:user_id`` must be listed in
  ``plugins.entries.web-terminal.settings.allowed_users`` — unset means nobody.
  The process-internal WS credential never opens a shell, whatever the list says.
* **Two shells at a time, counting shells still being torn down.** A slot is held
  until the PTY's close sequence has finished, so a reconnect loop cannot stack
  live-or-exiting shells past the cap. A close that raises is *not* proof the shell is
  gone: that slot stays quarantined (held) until the dashboard restarts.
* **Admission rate.** At most ``MAX_ADMISSIONS_PER_WINDOW`` shells are started per
  rolling ``ADMISSION_WINDOW_S`` (process-wide), so a connect/close loop cannot churn
  shells even when each one is reaped at once.
* **20 minutes without keyboard input** closes a shell. Resize controls are layout,
  not typing — every ``ESC[RESIZE:..]`` in a frame is consumed and none resets the
  clock. The idle watchdog runs on its own task, so a browser that stops reading
  output cannot stall it; an output send that stalls for 30 s also ends the shell.
* **Bounded input.** Dial-time size and every resize are clamped to the core resize
  ceiling (``PtyBridge.spawn`` forwards raw values to ptyprocess, where anything
  above 65535 raises inside the *forked child*); one input frame is at most 64 KiB;
  at most 1 MiB of input per rolling 10 s window. Every byte counts for at least the
  full 10 s (bounded accounting in 50 ms quanta, rounded *up*, so the window is
  10–10.05 s, never shorter). Residual: uvicorn's ``ws_max_size`` is the
  dashboard-wide 384 MiB attachment ceiling and a message is reassembled before this
  handler sees it; PTY output has no quota (a permitted command may print a lot).
* **Environment:** the dashboard environment **minus Hermes-managed secrets** (the
  same scrub the agent's own background commands get).
* **Audit log** (``$HERMES_HOME/logs/web-terminal-audit.log``, JSON lines):
  open / close / deny / error with peer, identity, size, pid, duration and byte
  counts — never command text or terminal output.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import datetime as _dt
import importlib
import inspect
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
PLUGIN_VERSION = "0.2.1"

MAX_TERMINALS = 2
IDLE_TIMEOUT_S = 20 * 60  # no keyboard input for this long -> the shell is closed (resize is not input)
OUTPUT_STALL_TIMEOUT_S = 30.0  # one PTY->browser send blocked for this long -> the shell is closed
# Dial-time size ceiling = hermes_cli.pty_bridge's resize clamp (_MAX_COLS / _MAX_ROWS), so the
# first layout and every later resize agree. spawn() itself does not clamp: a value > 65535
# reaches struct.pack in the forked child and the exception escapes there, not in this handler.
MAX_COLS = 2000
MAX_ROWS = 1000
# Terminal input is keystrokes or a paste. uvicorn's ws_max_size is the dashboard-wide
# attachment ceiling (384 MiB) and cannot be lowered per route, so bound it here.
MAX_INPUT_FRAME_BYTES = 64 * 1024  # ponytail: one frame; a paste larger than this closes the shell
INPUT_WINDOW_S = 10.0
INPUT_QUANTUM_S = 0.05  # accounting granularity; a byte is counted until its whole quantum is window-old
MAX_INPUT_BYTES_PER_WINDOW = 1024 * 1024
# Process-wide shell-start rate (in addition to the concurrency cap): reconnect churn bound.
MAX_ADMISSIONS_PER_WINDOW = 12
ADMISSION_WINDOW_S = 60.0
_READ_TIMEOUT_S = 0.2
_IDLE_BACKOFF_S = 0.05  # quiet terminal: yield instead of re-polling at once (mirrors core)
_WATCHDOG_TICK_S = 1.0  # idle watchdog cadence (shortened automatically for a short IDLE_TIMEOUT_S)
_CLOSE_TIMEOUT_S = 5.0  # bound on a WebSocket close handshake against a stalled peer

# Same resize escape the dashboard's xterm.js sends (web_server_chat._RESIZE_RE).
_RESIZE_RE = re.compile(rb"\x1b\[RESIZE:(\d+);(\d+)\]")

# Close codes mirror hermes_cli/web_routers/chat_ws.py::_ws_gate so logs and the
# browser banner agree on the cause. Auth/origin/disabled refusals happen before
# accept() (the browser sees a 403 handshake); the rest are post-accept closes.
CLOSE_AUTH = 4401        # bad / missing credential, or the core auth API is unavailable
CLOSE_ORIGIN = 4403      # host / origin / peer not allowed, or profile routing attempted
CLOSE_FORBIDDEN = 4403   # authenticated, but not in allowed_users (same code as /api/pty "not allowed")
CLOSE_DISABLED = 4404    # plugin not (or no longer) in plugins.enabled, or policy unreadable
CLOSE_IDLE = 4409        # closed after IDLE_TIMEOUT_S without keyboard input
CLOSE_EXITED = 4410      # shell exited (mirrors /api/pty)
CLOSE_BUSY = 1013        # terminal limit reached / PTY input stalled / output to the browser stalled
CLOSE_ERROR = 1011       # shell could not start
CLOSE_TOO_BIG = 1009     # one input frame over MAX_INPUT_FRAME_BYTES
CLOSE_POLICY = 1008      # more than MAX_INPUT_BYTES_PER_WINDOW within the sliding window

_CORE_MODULE = "hermes_cli.web_server_chat"
_BRIDGE_REQUIRED = ("spawn", "is_available", "read", "write", "resize", "close", "is_alive")

# Live terminals: slot key -> bookkeeping. A slot is held from admission until the PTY's
# close sequence has FINISHED (the reaper thread releases it), so "active" means
# live-or-closing. Mutated from the event loop and from reaper threads -> lock.
_ACTIVE: dict[int, dict[str, Any]] = {}
_ACTIVE_LOCK = threading.Lock()
_SEQ = itertools.count(1)


# --- Core contract -------------------------------------------------------------

def _core() -> Optional[Any]:
    """The dashboard's chat/PTY module, or ``None`` when it no longer exposes the
    private API this plugin depends on — in the shape this plugin was written against.

    Resolved on every call, never cached, so a failure cannot be masked by an
    earlier success. ``None`` means *refuse* — the terminal is the one feature
    that must not degrade to "open" when an upstream refactor moves the gate.
    Both gates must be plain synchronous callables: an ``async`` gate would hand
    back a coroutine object, which is truthy and would read as "allowed".
    """
    try:
        mod = importlib.import_module(_CORE_MODULE)
    except Exception:
        return None
    for name in ("_ws_auth_reason", "_ws_request_is_allowed"):
        fn = getattr(mod, name, None)
        if not callable(fn) or inspect.iscoroutinefunction(fn):
            return None
    bridge = getattr(mod, "PtyBridge", None)
    if bridge is None or any(not callable(getattr(bridge, n, None)) for n in _BRIDGE_REQUIRED):
        return None
    if not isinstance(getattr(mod, "PtyUnavailableError", None), type):
        return None
    return mod


def _ws_deny_reason(ws: WebSocket, core: Any) -> tuple[Optional[int], str]:
    """``(close_code, detail)`` when the upgrade must be refused, else ``(None, credential)``.

    Gate order and codes match ``_ws_gate``: credential first, then host/origin/peer.
    An exception inside either core gate is a refusal, not a pass; so is any result
    that is not exactly the documented shape (``(reason, credential)`` / ``True``).
    """
    try:
        result = core._ws_auth_reason(ws)
    except Exception as exc:  # a gate crash
        return CLOSE_AUTH, f"auth gate error: {type(exc).__name__}"
    if not (isinstance(result, tuple) and len(result) == 2):
        return CLOSE_AUTH, "auth gate error: unexpected result shape"
    auth_reason, cred = result
    # Contract: tuple[Optional[str], str]. Validate member types before using or logging them.
    if auth_reason is not None and not isinstance(auth_reason, str):
        return CLOSE_AUTH, "auth gate error: unexpected result shape"
    if auth_reason is not None:
        return CLOSE_AUTH, f"auth: {auth_reason}"
    if not isinstance(cred, str) or not cred.strip():
        return CLOSE_AUTH, "auth gate error: unexpected result shape"
    try:
        allowed = core._ws_request_is_allowed(ws)
    except Exception as exc:
        return CLOSE_ORIGIN, f"origin gate error: {type(exc).__name__}"
    if inspect.iscoroutine(allowed):
        allowed.close()  # never awaited; a coroutine object is truthy, so it must not be judged by truth
        return CLOSE_ORIGIN, "origin gate error: unexpected result shape"
    if allowed is not True:  # exactly True: a tuple, an int or None is contract drift, not "allowed"
        return CLOSE_ORIGIN, "host/origin/peer not allowed"
    return None, cred


# --- Policy (launch-home config, readable or nothing) --------------------------

def _policy_config() -> Optional[dict[str, Any]]:
    """The launch home's effective config, or ``None`` when it must not be trusted.

    ``None`` (-> the caller denies) when: the config/plugins API cannot be imported; the
    request is scoped to another profile's home (``?profile=`` routing sets a context-local
    HERMES_HOME override — policy is only ever the dashboard's own); or the read is a
    ``FailedConfigRead``, which is what Hermes serves after a parse/read failure: the
    last-known-good or default config, i.e. stale permissions. Re-read on every call.
    """
    try:
        from hermes_cli.config import load_config_readonly
        from hermes_cli.config_read_errors import FailedConfigRead
        from hermes_constants import get_hermes_home_override, get_process_hermes_home
    except Exception:
        return None
    override = get_hermes_home_override()
    if override is not None:
        try:
            if Path(override).resolve() != Path(get_process_hermes_home()).resolve():
                return None
        except Exception:
            return None
    try:
        cfg = load_config_readonly()
    except Exception:
        return None
    if isinstance(cfg, FailedConfigRead) or not isinstance(cfg, dict):
        return None
    return cfg


def _plugin_enabled() -> bool:
    """Mirror ``web_server._plugin_api_runtime_gate`` for the WebSocket leg (HTTP middleware
    does not run for WS upgrades): the plugin must be in ``plugins.enabled`` and not in
    ``plugins.disabled`` — of a readable launch-home config. Anything else reads as disabled."""
    cfg = _policy_config()
    if cfg is None:
        return False
    plugins = cfg.get("plugins") or {}
    enabled = plugins.get("enabled")
    disabled = plugins.get("disabled") or []
    if not isinstance(enabled, list) or not isinstance(disabled, list):
        return False
    return PLUGIN_NAME in enabled and PLUGIN_NAME not in disabled


def _allowed_users() -> frozenset[str]:
    """``plugins.entries.web-terminal.settings.allowed_users`` as ``provider:user_id`` strings
    (e.g. ``basic:alice``). Unset, malformed, unreadable or foreign-scoped config -> empty ->
    nobody: the shell stays off until the operator names who may open it."""
    cfg = _policy_config()
    if cfg is None:
        return frozenset()
    entries = (cfg.get("plugins") or {}).get("entries") or {}
    raw = ((entries.get(PLUGIN_NAME) or {}).get("settings") or {}).get("allowed_users")
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
    No identity (``--insecure`` loopback mode stamps none) -> False."""
    provider, user = identity.get("provider"), identity.get("user_id")
    if not (isinstance(provider, str) and provider and isinstance(user, str) and user):
        return False
    return f"{provider}:{user}" in _allowed_users()


def _profile_routed(params: Any) -> bool:
    """A non-empty ``?profile=`` names another profile's config/secret scope. The shell belongs
    to the dashboard's own home only; such a request is refused before any policy read."""
    return bool((params.get("profile") or "").strip())


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


class _InputBudget:
    """True rolling-window byte budget with bounded memory.

    Bytes are accumulated per ``quantum_s`` quantum. A quantum is dropped only once its
    END is a full ``window_s`` old, so every byte is counted for at least ``window_s``
    (at most ``window_s + quantum_s``) — the window never expires early. Memory is
    O(window_s / quantum_s) entries whatever the frame count or size."""

    def __init__(self, window_s: float, limit: int, quantum_s: float = INPUT_QUANTUM_S) -> None:
        self.window = window_s
        self.quantum = quantum_s
        self.limit = limit
        self.entries: collections.deque[list[int]] = collections.deque()  # [quantum index, bytes]
        self.total = 0

    def add(self, now: float, nbytes: int) -> bool:
        """Record ``nbytes`` at ``now``; True when the window total now exceeds the limit."""
        q = int(now // self.quantum)
        while self.entries and (self.entries[0][0] + 1) * self.quantum + self.window <= now:
            self.total -= self.entries.popleft()[1]
        if self.entries and self.entries[-1][0] >= q:
            self.entries[-1][1] += nbytes  # same (or clock-skewed earlier) quantum: merge
        else:
            self.entries.append([q, nbytes])
        self.total += nbytes
        return self.total > self.limit


class _AdmissionRate:
    """At most ``limit`` admissions per rolling ``window_s``; O(limit) memory."""

    def __init__(self, window_s: float, limit: int) -> None:
        self.window = window_s
        self.limit = limit
        self.stamps: collections.deque[float] = collections.deque()
        self.lock = threading.Lock()

    def take(self, now: float) -> bool:
        """Consume one admission at ``now``; False when the window is full."""
        with self.lock:
            while self.stamps and self.stamps[0] + self.window <= now:
                self.stamps.popleft()
            if len(self.stamps) >= self.limit:
                return False
            self.stamps.append(now)
            return True


_ADMISSIONS = _AdmissionRate(ADMISSION_WINDOW_S, MAX_ADMISSIONS_PER_WINDOW)


def _reserve_slot(peer: str) -> tuple[Optional[int], Optional[dict[str, Any]]]:
    """Admit one shell if a slot is free. Check-and-reserve under the lock: the reaper
    threads release slots concurrently with admission."""
    with _ACTIVE_LOCK:
        if len(_ACTIVE) >= MAX_TERMINALS:
            return None, None
        key = next(_SEQ)
        slot: dict[str, Any] = {"key": key, "peer": peer, "opened": time.time(), "pid": None,
                                "reaper": None}
        _ACTIVE[key] = slot
        return key, slot


def _active_count() -> int:
    with _ACTIVE_LOCK:
        return len(_ACTIVE)


def _quarantined_count() -> int:
    with _ACTIVE_LOCK:
        return sum(1 for s in _ACTIVE.values() if s.get("quarantined"))


def _release_slot(key: int) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.pop(key, None)


def _cleanup_unconfirmed(bridge: Any) -> Optional[str]:
    """None only if the bridge's own child is positively confirmed gone; otherwise a reason.

    Relies on the private core contract ``PtyBridge._proc`` (a ptyprocess handle whose
    ``isalive()`` reaps via waitpid, independent of the bridge's early ``_closed`` flag) and
    ``_pgid``. Missing or failing contract -> unconfirmed (fail closed). When the shell leads
    its own process group, the group must also be empty. A shared-group child's descendants
    cannot be checked without a group signal, so only the leader is confirmed there."""
    proc = getattr(bridge, "_proc", None)
    isalive = getattr(proc, "isalive", None)
    if not callable(isalive):
        return "cleanup contract missing"
    try:
        if isalive() is not False:
            return "child still alive"
    except Exception as exc:  # health: allow BLE001 -- unknown status is not proof
        return f"child status unknown: {type(exc).__name__}"
    pgid = getattr(bridge, "_pgid", None)
    pid = getattr(proc, "pid", None)
    if isinstance(pgid, int) and not isinstance(pgid, bool) and pgid > 1 and pgid == pid:
        try:
            os.killpg(pgid, 0)  # windows-footgun: ok — POSIX-only, as the core bridge
        except ProcessLookupError:
            return None
        except Exception as exc:  # health: allow BLE001 -- EPERM etc.: group may exist
            return f"process group status unknown: {type(exc).__name__}"
        return "process group still populated"
    return None


def _reap(slot: dict[str, Any], bridge: Any) -> threading.Thread:
    """Close the PTY bridge off the event loop on a plain thread — never an executor
    future — and release the slot only AFTER the close sequence has finished.

    ``bridge.close()`` blocks (SIGHUP → SIGTERM → SIGKILL cadence), so it must not run on
    the loop; but a ``run_in_executor``/``to_thread`` future is cancelled along with the
    awaiting task at shutdown and a not-yet-started work item is silently dropped. The slot
    is released only after ``_cleanup_unconfirmed`` positively confirms the child is gone. A daemon thread cannot be cancelled. Single-flight per
    slot: ``PtyBridge.close`` returns at once for a second concurrent caller, so a second
    reaper must not be the one that releases the slot while the first is still signalling."""
    with _ACTIVE_LOCK:
        thread = slot.get("reaper")
        if thread is not None:
            return thread

        def _quarantine(reason: str) -> None:
            slot["quarantined"] = True
            _log.error("web-terminal reap unconfirmed pid=%s: %s; slot quarantined until restart",
                       slot.get("pid"), reason)
            _audit("error", reason=f"reap: {reason}", pid=slot.get("pid"), quarantined=True)

        def _close() -> None:
            # Release ONLY on positive proof the owned child is gone. A normal return from
            # close() is not proof: the core sets its closed flag first and suppresses a failed
            # final terminate, so bridge.is_alive() is no proof either. Unknown -> hold the slot.
            try:
                bridge.close()
            except Exception as exc:  # health: allow BLE001 -- quarantine, never free capacity
                _quarantine(type(exc).__name__)
                return
            reason = _cleanup_unconfirmed(bridge)
            if reason is not None:
                _quarantine(reason)
                return
            _release_slot(slot["key"])

        thread = threading.Thread(target=_close, name="web-terminal-reap", daemon=True)
        slot["reaper"] = thread
    thread.start()
    return thread


async def _close_ws(ws: WebSocket, code: int, reason: str) -> None:
    """Bounded close: a peer that has stopped reading can hold the close handshake open."""
    with contextlib.suppress(Exception):
        await asyncio.wait_for(ws.close(code=code, reason=reason[:120]), _CLOSE_TIMEOUT_S)


async def _fail(ws: WebSocket, message: str, code: int = CLOSE_ERROR) -> None:
    """Post-accept failure: tell the user why (red line in xterm), then close."""
    with contextlib.suppress(Exception):
        await asyncio.wait_for(ws.send_text(f"\r\n\x1b[31m{message}\x1b[0m\r\n"), _CLOSE_TIMEOUT_S)
    await _close_ws(ws, code, message)


# --- Routes --------------------------------------------------------------------

@router.get("/health")
async def health(request: Request):
    if _profile_routed(request.query_params):
        return JSONResponse({"error": "profile routing not supported"}, status_code=403)
    core = _core()
    pty_ok = bool(core is not None and core.PtyBridge.is_available())
    return {
        "ok": core is not None and pty_ok,
        "contract_ok": core is not None,
        "pty_available": pty_ok,
        "policy_readable": _policy_config() is not None,
        "allowed_users_configured": bool(_allowed_users()),
        "active": _active_count(),
        "quarantined": _quarantined_count(),
        "limit": MAX_TERMINALS,
        "idle_timeout_seconds": IDLE_TIMEOUT_S,
        "version": PLUGIN_VERSION,
    }


@router.get("/shell")
async def shell_info(request: Request):
    """Preflight for the web bridge: which shell/cwd a terminal would get. Same refusals as
    the dial — no profile routing, owner only — the host's working directory is not for every
    dashboard login, and never evaluated against another profile's allow-list."""
    if _profile_routed(request.query_params):
        return JSONResponse({"error": "profile routing not supported"}, status_code=403)
    if not _user_allowed(_http_identity(request)):
        return JSONResponse({"error": "not allowed"}, status_code=403)
    return {"shell": os.path.basename(_shell_argv()[0]), "cwd": os.getcwd()}


@router.websocket("/terminal")
async def terminal_ws(ws: WebSocket) -> None:
    peer = ws.client.host if ws.client else "?"

    # 1. No profile routing — before ANY policy read. The dashboard mounts every plugin
    #    route behind a ``?profile=`` scope dependency (HTTP and WS alike); by the time this
    #    handler runs, a ``?profile=X`` request already sits in profile X's config/secret
    #    scope. The handler must then neither read policy nor spawn: refuse.
    if _profile_routed(ws.query_params):
        _audit("deny", reason="profile_param", peer=peer)
        _log.warning("web-terminal refused: profile routing not supported peer=%s", peer)
        await ws.close(code=CLOSE_ORIGIN, reason="profile routing not supported")
        return

    # 2. Private-API contract (fail closed: no core gate -> no terminal, ever).
    core = _core()
    if core is None:
        _audit("deny", reason="core_contract", peer=peer)
        _log.warning("web-terminal refused: dashboard private API unavailable or changed "
                     "(failing closed) peer=%s", peer)
        await ws.close(code=CLOSE_AUTH, reason="terminal unavailable: dashboard API contract")
        return

    # 3. Still enabled, per a READABLE launch-home config? (the HTTP-only runtime gate does
    #    not cover WS upgrades; a last-known-good fallback does not count as enabled)
    if not _plugin_enabled():
        _audit("deny", reason="disabled", peer=peer)
        _log.warning("web-terminal refused: plugin disabled or policy unreadable peer=%s", peer)
        await ws.close(code=CLOSE_DISABLED, reason="web-terminal plugin disabled")
        return

    # 4. Credential, then host/origin/peer — the dashboard's own gates.
    code, detail = _ws_deny_reason(ws, core)
    if code is not None:
        _audit("deny", reason=detail, peer=peer)
        _log.warning("web-terminal refused: %s peer=%s", detail, peer)
        await ws.close(code=code, reason=detail[:120])
        return
    cred = detail
    ident = _identity(ws)

    # 5. A shell comes from a browser-minted ticket or a verified user session — never from
    #    the process-internal credential the server hands its own WS clients.
    if cred == "internal":
        _audit("deny", reason="internal_credential", peer=peer, **ident)
        _log.warning("web-terminal refused: internal credential cannot open a shell peer=%s", peer)
        await ws.close(code=CLOSE_FORBIDDEN, reason="not allowed to open a terminal")
        return

    # 6. Owner gate. The credential proved *a* dashboard user; a shell on the host needs
    #    *the* operator. Identity is whatever the core gate stamped — never a client parameter.
    if not _user_allowed(ident):
        _audit("deny", reason="not_allowed", peer=peer, **ident)
        _log.warning("web-terminal refused: user not in allowed_users peer=%s", peer)
        await ws.close(code=CLOSE_FORBIDDEN, reason="not allowed to open a terminal")
        return

    # 7. Concurrency cap — live AND still-closing shells count.
    key, slot = _reserve_slot(peer)
    if key is None or slot is None:
        _audit("deny", reason="limit", peer=peer, active=_active_count(), limit=MAX_TERMINALS, **ident)
        _log.warning("web-terminal refused: limit %d reached peer=%s", MAX_TERMINALS, peer)
        await ws.accept()
        await _fail(ws, f"terminal limit reached ({MAX_TERMINALS} open shells)", code=CLOSE_BUSY)
        return

    # 8. Admission rate — bounds connect/close churn even when every shell is reaped at once.
    if not _ADMISSIONS.take(time.monotonic()):
        _release_slot(key)
        _audit("deny", reason="admission_rate", peer=peer, limit=MAX_ADMISSIONS_PER_WINDOW,
               window_s=ADMISSION_WINDOW_S, **ident)
        _log.warning("web-terminal refused: admission rate %d/%ss reached peer=%s",
                     MAX_ADMISSIONS_PER_WINDOW, ADMISSION_WINDOW_S, peer)
        await ws.accept()
        await _fail(ws, "too many terminals opened recently; try again shortly", code=CLOSE_BUSY)
        return

    bridge = None
    tasks: list[asyncio.Task] = []
    loop = asyncio.get_running_loop()
    state = {"last_input": loop.time(), "bytes_in": 0, "bytes_out": 0, "close": "client_disconnect"}
    budget = _InputBudget(INPUT_WINDOW_S, MAX_INPUT_BYTES_PER_WINDOW)
    teardown = asyncio.Event()  # set once the session is over, whatever the peer does next

    async def _end(reason: str, code: int, text: str) -> None:
        """Shared teardown trigger for the pump / watchdog / writer: record the first reason,
        start the PTY reap at once, attempt a bounded close (so a live browser sees the code
        and reason), then release the writer. A peer that has stopped reading may never
        complete the close handshake or deliver a disconnect; the writer must not wait for
        either — when the handler returns, the server closes the transport itself."""
        if state["close"] == "client_disconnect":
            state["close"] = reason
        if bridge is not None:
            _reap(slot, bridge)
        await _close_ws(ws, code, text)
        teardown.set()

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
            _audit("error", reason=f"spawn: {type(exc).__name__}", peer=peer, **ident)
            await _fail(ws, "terminal could not start (see the dashboard log)")
            return
        slot["pid"] = getattr(bridge, "pid", None)
        _audit("open", peer=peer, cred=cred, pid=slot["pid"], cols=cols, rows=rows, cwd=cwd,
               active=_active_count(), **ident)
        _log.info("web-terminal opened peer=%s cred=%s pid=%s", peer, cred, slot["pid"])

        async def pump_pty_to_ws() -> None:
            while True:
                # bridge.read blocks up to the poll timeout in a worker thread; None = EOF.
                try:
                    chunk = await asyncio.to_thread(bridge.read, _READ_TIMEOUT_S)
                except Exception as exc:  # health: allow BLE001 -- PTY I/O error: shared bounded teardown
                    _log.warning("web-terminal PTY read failed: %s", type(exc).__name__)
                    await _end("read_error", CLOSE_ERROR, "terminal output failed")
                    return
                if chunk is None:
                    await _end("shell_exited", CLOSE_EXITED, "shell exited")
                    return
                if not chunk:
                    await asyncio.sleep(_IDLE_BACKOFF_S)
                    continue
                state["bytes_out"] += len(chunk)
                try:
                    await asyncio.wait_for(ws.send_bytes(chunk), OUTPUT_STALL_TIMEOUT_S)
                except asyncio.TimeoutError:
                    await _end("output_stalled", CLOSE_BUSY, "browser stopped reading output")
                    return
                except Exception:
                    # The socket is gone or another task already closed it for a reason of
                    # its own (flood, oversized frame, idle); keep that reason for the audit.
                    await _end("send_failed", CLOSE_BUSY, "send failed")
                    return

        async def idle_watchdog() -> None:
            # Its own task on purpose: a pump blocked in send_bytes() (browser not reading)
            # must not be able to stall the idle check.
            while True:
                await asyncio.sleep(max(0.01, min(_WATCHDOG_TICK_S, IDLE_TIMEOUT_S / 4)))
                if loop.time() - state["last_input"] > IDLE_TIMEOUT_S:
                    await _end("idle_timeout", CLOSE_IDLE,
                               f"closed after {IDLE_TIMEOUT_S // 60} min without input")
                    return

        tasks = [asyncio.create_task(pump_pty_to_ws()), asyncio.create_task(idle_watchdog())]
        stop = asyncio.create_task(teardown.wait())
        tasks.append(stop)

        while True:
            recv = asyncio.create_task(ws.receive())
            tasks.append(recv)  # owned: cancelled + awaited in finally, even if this handler is cancelled
            done, _ = await asyncio.wait({recv, stop}, return_when=asyncio.FIRST_COMPLETED)
            if recv not in done:
                # The session was ended by the pump or the watchdog. Do not wait for the
                # peer's disconnect (a stalled peer never sends one): leave, so the server
                # closes the transport when this handler returns.
                recv.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await recv
                break
            tasks.remove(recv)
            try:
                msg = recv.result()
            except Exception:  # health: allow BLE001 -- receive() after close, or a transport error: the session is over
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
                await _end("input_too_large", CLOSE_TOO_BIG, "input frame too large")
                break
            now = loop.time()
            if budget.add(now, len(raw)):
                await _end("input_flood", CLOSE_POLICY, "input rate limit")
                break
            # Resize escapes ride the input stream: consume EVERY one, apply the last valid
            # size, forward the rest. Resize-only frames are layout, not typing: they never
            # reset the idle clock.
            size: Optional[tuple[int, int]] = None
            for m in _RESIZE_RE.finditer(raw):
                c, r = _dimension(m.group(1), 0, MAX_COLS), _dimension(m.group(2), 0, MAX_ROWS)
                if c and r:
                    size = (c, r)
            if size is not None:
                bridge.resize(*size)
            raw = _RESIZE_RE.sub(b"", raw)
            if not raw:
                continue
            state["last_input"] = now
            state["bytes_in"] += len(raw)
            if not await bridge.write(raw):
                await _end("input_stalled", CLOSE_BUSY, "PTY input stalled")
                break
    except WebSocketDisconnect:
        pass
    finally:
        # Cancellation-safe teardown: the handler task is cancelled on server shutdown
        # (and by Starlette's TestClient), and a cancelled task gets CancelledError at
        # its next await. So: every must-happen step runs synchronously FIRST — audit
        # line, task cancel, and the reap on a plain thread (never an executor future,
        # which shutdown would discard; it releases the slot when the PTY is gone) — and
        # only then the best-effort await of the tasks.
        if bridge is None:
            _release_slot(key)  # nothing to reap: the slot is free now
        else:
            _audit("close", peer=peer, pid=slot["pid"], reason=state["close"],
                   duration_s=round(time.time() - slot["opened"], 1),
                   bytes_in=state["bytes_in"], bytes_out=state["bytes_out"],
                   active=_active_count(), **ident)
            _log.info("web-terminal closed peer=%s pid=%s reason=%s", peer, slot["pid"], state["close"])
        for t in tasks:
            t.cancel()
        if bridge is not None:
            _reap(slot, bridge)
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
