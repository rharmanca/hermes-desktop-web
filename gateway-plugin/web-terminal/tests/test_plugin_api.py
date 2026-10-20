"""Tests for the web-terminal dashboard plugin (gateway-plugin/web-terminal/dashboard/plugin_api.py).

Run with the Hermes venv so the real ``hermes_cli`` is importable for the contract tests
(``PYTHONPATH`` = the hermes-agent checkout, as the dashboard itself puts that root on ``sys.path``):

    cd <repo> && HERMES_HOME=~/.hermes PYTHONPATH=<hermes-agent> \
        <hermes-agent>/.venv/bin/python -m pytest gateway-plugin/web-terminal/tests -q -o addopts=""
"""

from __future__ import annotations

import importlib.util
import json
import queue
import sys
import time
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

PLUGIN_FILE = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
PREFIX = "/api/plugins/web-terminal"
WS = PREFIX + "/terminal"

# Identities the fake core gate stamps on the socket (the way the real gate does), and the
# allow-list the fixture installs: only ``test:owner`` may open a shell.
_OWNER = {"user_id": "owner", "provider": "test"}
_GUEST = {"user_id": "guest", "provider": "test"}


def _load_plugin(name: str = "hermes_dashboard_plugin_web_terminal_test"):
    spec = importlib.util.spec_from_file_location(name, PLUGIN_FILE)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeBridge:
    """Stand-in for hermes_cli.pty_bridge.PtyBridge: echoes writes back as output."""

    spawned: list["FakeBridge"] = []

    def __init__(self, argv, cwd, env, cols, rows):
        self.argv, self.cwd, self.env, self.size = argv, cwd, env, (cols, rows)
        self.pid = 4242 + len(FakeBridge.spawned)
        self.written = bytearray()
        self.out: "queue.Queue[bytes | None]" = queue.Queue()
        self.closed = False
        self.accept_input = True
        FakeBridge.spawned.append(self)

    @classmethod
    def spawn(cls, argv, *, cwd=None, env=None, cols=80, rows=24):
        return cls(argv, cwd, env, cols, rows)

    @classmethod
    def is_available(cls) -> bool:
        return True

    def read(self, timeout: float):
        if self.closed:
            return None
        try:
            return self.out.get(timeout=timeout)
        except queue.Empty:
            return b""

    async def write(self, data: bytes) -> bool:
        if not self.accept_input:
            return False
        self.written.extend(data)
        self.out.put(b"echo:" + bytes(data))
        return True

    def resize(self, cols: int, rows: int) -> None:
        self.size = (cols, rows)

    def is_alive(self) -> bool:
        return not self.closed

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.out.put(None)

    def exit_shell(self) -> None:
        """Simulate the shell exiting on its own (EOF on the PTY master)."""
        self.closed = True
        self.out.put(None)


def _stamping_auth(identity, auth=(None, "test")):
    """A ``_ws_auth_reason`` stand-in that stamps ``identity`` the way the real gate does."""
    def _auth(ws):
        if identity is not None:
            ws._hermes_auth_identity = dict(identity)
        return auth
    return _auth


def _fake_core(auth=(None, "test"), allowed=True, identity=_OWNER, bridge=None):
    return types.SimpleNamespace(
        _ws_auth_reason=_stamping_auth(identity, auth),
        _ws_request_is_allowed=lambda ws: allowed,
        PtyBridge=bridge or FakeBridge,
        PtyUnavailableError=RuntimeError,
    )


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    """Plugin module wired to a fake core gate + fake PTY, audit log under a temp HERMES_HOME,
    ``test:owner`` as the only allowed user."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    FakeBridge.spawned.clear()
    mod = _load_plugin()
    mod._ACTIVE.clear()
    monkeypatch.setattr(mod, "_plugin_enabled", lambda: True)
    monkeypatch.setattr(mod, "_core", lambda: _fake_core())
    monkeypatch.setattr(mod, "_allowed_users", lambda: frozenset({"test:owner"}))
    yield mod
    mod._ACTIVE.clear()


@pytest.fixture
def client(plugin):
    app = FastAPI()
    app.include_router(plugin.router, prefix=PREFIX)
    with TestClient(app) as c:
        yield c


def _audit_lines(tmp_path) -> list[dict]:
    path = tmp_path / "hermes-home" / "logs" / "web-terminal-audit.log"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _wait_closed(bridge: FakeBridge, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not bridge.closed and time.monotonic() < deadline:
        time.sleep(0.02)
    assert bridge.closed, "bridge was not reaped"


def _wait_no_active(plugin, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while plugin._ACTIVE and time.monotonic() < deadline:
        time.sleep(0.02)
    assert plugin._ACTIVE == {}


# --- Contract (against the REAL hermes_cli, so an upgrade that moves the gate fails here first) ---

def test_real_core_still_satisfies_contract():
    mod = _load_plugin("hermes_dashboard_plugin_web_terminal_contract")
    core = mod._core()
    assert core is not None, "hermes_cli.web_server_chat no longer exposes the private API this plugin needs"
    assert callable(core._ws_auth_reason) and callable(core._ws_request_is_allowed)
    assert core.PtyBridge.is_available() is True


def test_enabled_check_uses_real_config_and_fails_closed(monkeypatch, tmp_path):
    mod = _load_plugin("hermes_dashboard_plugin_web_terminal_enabled")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "empty-home"))
    assert mod._plugin_enabled() is False  # not in plugins.enabled of this (empty) home
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins_cmd", None)  # import -> ImportError
    assert mod._plugin_enabled() is False


def test_contract_fails_closed_when_core_import_breaks(monkeypatch):
    mod = _load_plugin("hermes_dashboard_plugin_web_terminal_contract2")
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_chat", None)  # import -> ImportError
    assert mod._core() is None


def test_contract_fails_closed_when_gate_renamed(monkeypatch):
    mod = _load_plugin("hermes_dashboard_plugin_web_terminal_contract3")
    stub = types.ModuleType("hermes_cli.web_server_chat")
    stub._ws_request_is_allowed = lambda ws: True  # but no _ws_auth_reason
    stub.PtyBridge = FakeBridge
    stub.PtyUnavailableError = RuntimeError
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_chat", stub)
    assert mod._core() is None
    stub._ws_auth_reason = lambda ws: (None, "x")
    stub.PtyBridge = None  # bridge import failed upstream
    assert mod._core() is None


def test_real_bridge_spawns_at_the_clamped_ceiling():
    """The dial clamp lands inside what ptyprocess can represent: a 70000x70000 request becomes
    MAX_COLS x MAX_ROWS and a REAL PTY reports exactly that size. (Unclamped, > 65535 raises
    ``struct.error`` inside the forked child — see the review's probe_bridge_dimensions.py.)"""
    mod = _load_plugin("hermes_dashboard_plugin_web_terminal_realpty")
    core = mod._core()
    assert core is not None and core.PtyBridge.is_available()
    cols = mod._dimension("70000", 80, mod.MAX_COLS)
    rows = mod._dimension("70000", 24, mod.MAX_ROWS)
    assert (cols, rows) == (mod.MAX_COLS, mod.MAX_ROWS) and max(cols, rows) <= 65535
    bridge = core.PtyBridge.spawn(["/bin/sh", "-c", "stty size"], cwd=None,
                                  env={"PATH": "/usr/bin:/bin", "TERM": "xterm"}, cols=cols, rows=rows)
    out = bytearray()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            chunk = bridge.read(0.2)
            if chunk is None:
                break
            out.extend(chunk)
    finally:
        bridge.close()
    assert f"{rows} {cols}".encode() in bytes(out), bytes(out)


# --- Pre-accept refusals -------------------------------------------------------

def _expect_refused(client, code: int, url: str = WS):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(url):
            pass
    assert exc.value.code == code


def test_refuses_without_core_contract(client, plugin, monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, "_core", lambda: None)
    _expect_refused(client, plugin.CLOSE_AUTH)
    assert FakeBridge.spawned == []
    assert [e["reason"] for e in _audit_lines(tmp_path) if e["event"] == "deny"] == ["core_contract"]


def test_refuses_when_plugin_disabled(client, plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_plugin_enabled", lambda: False)
    _expect_refused(client, plugin.CLOSE_DISABLED)
    assert FakeBridge.spawned == []


def test_refuses_bad_or_missing_credential(client, plugin, monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, "_core", lambda: _fake_core(auth=("no_credential", "none")))
    _expect_refused(client, plugin.CLOSE_AUTH)
    monkeypatch.setattr(plugin, "_core", lambda: _fake_core(auth=("ticket_invalid", "ticket")))
    _expect_refused(client, plugin.CLOSE_AUTH, WS + "?ticket=stale")
    assert FakeBridge.spawned == []
    reasons = [e["reason"] for e in _audit_lines(tmp_path) if e["event"] == "deny"]
    assert reasons == ["auth: no_credential", "auth: ticket_invalid"]


def test_refuses_when_core_gate_crashes_or_changes_shape(client, plugin, monkeypatch):
    def boom(ws):
        raise RuntimeError("gate exploded")
    monkeypatch.setattr(plugin, "_core", lambda: types.SimpleNamespace(
        _ws_auth_reason=boom, _ws_request_is_allowed=lambda ws: True,
        PtyBridge=FakeBridge, PtyUnavailableError=RuntimeError))
    _expect_refused(client, plugin.CLOSE_AUTH)
    # A gate that now returns a bare bool instead of (reason, cred) must also refuse.
    monkeypatch.setattr(plugin, "_core", lambda: types.SimpleNamespace(
        _ws_auth_reason=lambda ws: True, _ws_request_is_allowed=lambda ws: True,
        PtyBridge=FakeBridge, PtyUnavailableError=RuntimeError))
    _expect_refused(client, plugin.CLOSE_AUTH)
    assert FakeBridge.spawned == []


def test_refuses_bad_origin(client, plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_core", lambda: _fake_core(allowed=False))
    _expect_refused(client, plugin.CLOSE_ORIGIN)
    assert FakeBridge.spawned == []


def test_refuses_profile_routing(client, plugin, tmp_path):
    _expect_refused(client, plugin.CLOSE_ORIGIN, WS + "?profile=work-primary")
    assert FakeBridge.spawned == []
    assert [e["reason"] for e in _audit_lines(tmp_path) if e["event"] == "deny"] == ["profile_param"]


# --- Owner gate (a dashboard login is not enough for a host shell) --------------

def test_refuses_authenticated_non_owner(client, plugin, monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, "_core", lambda: _fake_core(identity=_GUEST))
    _expect_refused(client, plugin.CLOSE_FORBIDDEN)
    assert FakeBridge.spawned == []
    denies = [e for e in _audit_lines(tmp_path) if e["event"] == "deny"]
    assert denies == [{**denies[0], "reason": "not_allowed", "user_id": "guest", "provider": "test"}]


def test_refuses_when_the_gate_stamped_no_identity(client, plugin, monkeypatch):
    # Loopback / --insecure mode stamps nothing: nobody is the owner there.
    monkeypatch.setattr(plugin, "_core", lambda: _fake_core(identity=None))
    _expect_refused(client, plugin.CLOSE_FORBIDDEN)
    assert FakeBridge.spawned == []


def test_identity_is_the_server_stamp_never_a_client_parameter(client, plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_core", lambda: _fake_core(identity=_GUEST))
    _expect_refused(client, plugin.CLOSE_FORBIDDEN, WS + "?user_id=owner&provider=test")
    assert FakeBridge.spawned == []


def test_refuses_everyone_until_allowed_users_is_configured(client, plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_allowed_users", lambda: frozenset())
    assert client.get(PREFIX + "/health").json()["allowed_users_configured"] is False
    _expect_refused(client, plugin.CLOSE_FORBIDDEN)  # even the would-be owner
    assert FakeBridge.spawned == []


def test_allowed_users_reads_the_real_config(monkeypatch, tmp_path):
    mod = _load_plugin("hermes_dashboard_plugin_web_terminal_allowed")
    empty = tmp_path / "empty-home"
    empty.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(empty))
    assert mod._allowed_users() == frozenset()  # no config at all -> nobody

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "plugins:\n  entries:\n    web-terminal:\n      settings:\n        allowed_users:\n"
        "          - basic:alice\n          - ' oidc:someone '\n          - nocolon\n          - 42\n",
        encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert mod._allowed_users() == frozenset({"basic:alice", "oidc:someone"})
    assert mod._user_allowed({"user_id": "alice", "provider": "basic"}) is True
    assert mod._user_allowed({"user_id": "alice", "provider": "oidc"}) is False  # same name, other provider
    assert mod._user_allowed({"user_id": None, "provider": "basic"}) is False
    assert mod._user_allowed({}) is False

    scalar = tmp_path / "scalar-home"
    scalar.mkdir()
    (scalar / "config.yaml").write_text(
        "plugins:\n  entries:\n    web-terminal:\n      settings:\n        allowed_users: basic:alice\n",
        encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(scalar))
    assert mod._allowed_users() == frozenset({"basic:alice"})

    malformed = tmp_path / "malformed-home"
    malformed.mkdir()
    (malformed / "config.yaml").write_text(
        "plugins:\n  entries:\n    web-terminal:\n      settings:\n        allowed_users: 7\n",
        encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(malformed))
    assert mod._allowed_users() == frozenset()


def test_shell_preflight_is_owner_gated(plugin):
    bare = FastAPI()
    bare.include_router(plugin.router, prefix=PREFIX)
    with TestClient(bare) as c:
        r = c.get(PREFIX + "/shell")  # no session stamped (e.g. --insecure mode)
        assert r.status_code == 403 and "cwd" not in r.json()

    gated = FastAPI()

    @gated.middleware("http")
    async def _stamp_session(request, call_next):
        request.state.session = types.SimpleNamespace(
            user_id=request.headers.get("x-test-user"), provider="test")
        return await call_next(request)

    gated.include_router(plugin.router, prefix=PREFIX)
    with TestClient(gated) as c:
        assert c.get(PREFIX + "/shell", headers={"x-test-user": "guest"}).status_code == 403
        info = c.get(PREFIX + "/shell", headers={"x-test-user": "owner"})
        assert info.status_code == 200 and info.json()["shell"] and info.json()["cwd"]


# --- Happy path ----------------------------------------------------------------

def test_shell_roundtrip_resize_exit_and_audit(client, plugin, tmp_path):
    typed = b"printf TYPED-ONLY-IN-PTY\r"
    with client.websocket_connect(WS + "?cols=120&rows=40&cwd=/nonexistent-dir") as conn:
        bridge = FakeBridge.spawned[-1]
        assert bridge.size == (120, 40)
        assert bridge.cwd is None  # stale cwd falls back, never fails the dial
        assert bridge.argv[-1] == "-l" and bridge.env.get("TERM")
        conn.send_bytes(typed)
        assert conn.receive_bytes() == b"echo:" + typed
        assert bytes(bridge.written) == typed
        # Resize escape is consumed, not written to the shell.
        conn.send_text("\x1b[RESIZE:100;30]")
        conn.send_bytes(b"x")
        assert conn.receive_bytes() == b"echo:x"
        assert bridge.size == (100, 30)
        assert bytes(bridge.written) == typed + b"x"
        # Shell exits -> 4410 like /api/pty.
        bridge.exit_shell()
        with pytest.raises(WebSocketDisconnect) as exc:
            conn.receive_bytes()
        assert exc.value.code == plugin.CLOSE_EXITED
    _wait_closed(bridge)
    _wait_no_active(plugin)

    events = _audit_lines(tmp_path)
    assert [e["event"] for e in events] == ["open", "close"]
    assert events[0]["pid"] == bridge.pid and events[0]["cols"] == 120 and events[0]["cred"] == "test"
    assert events[0]["user_id"] == "owner" and events[0]["provider"] == "test"
    assert events[1]["reason"] == "shell_exited"
    assert events[1]["bytes_in"] == len(typed) + 1 and events[1]["bytes_out"] > 0
    raw_log = (tmp_path / "hermes-home" / "logs" / "web-terminal-audit.log").read_text()
    assert "TYPED-ONLY-IN-PTY" not in raw_log and "echo:" not in raw_log  # no command/output content


def test_client_disconnect_reaps_shell(client, plugin, tmp_path):
    with client.websocket_connect(WS) as conn:
        bridge = FakeBridge.spawned[-1]
        conn.send_bytes(b"a")
        assert conn.receive_bytes() == b"echo:a"
    _wait_closed(bridge)
    _wait_no_active(plugin)
    assert [e["event"] for e in _audit_lines(tmp_path)] == ["open", "close"]


# --- Dimensions ----------------------------------------------------------------

def test_dial_dimensions_are_clamped_before_spawn(client, plugin, tmp_path):
    cases = {
        "?cols=70000&rows=70000": (plugin.MAX_COLS, plugin.MAX_ROWS),
        "?cols=2000&rows=1000": (2000, 1000),
        "?cols=0&rows=-5": (80, 24),
        "?cols=abc&rows=": (80, 24),
        "?cols=" + "9" * 5000 + "&rows=24": (80, 24),  # past int()'s digit limit: default, not an exception
    }
    for query, expected in cases.items():
        with client.websocket_connect(WS + query) as conn:
            bridge = FakeBridge.spawned[-1]
            assert bridge.size == expected, query
            assert max(bridge.size) <= 65535
            conn.send_bytes(b"k")
            assert conn.receive_bytes() == b"echo:k"
        _wait_closed(bridge)
        _wait_no_active(plugin)
    opens = [e for e in _audit_lines(tmp_path) if e["event"] == "open"]
    assert (opens[0]["cols"], opens[0]["rows"]) == (plugin.MAX_COLS, plugin.MAX_ROWS)


def test_resize_escape_is_clamped_and_zero_is_ignored(client, plugin):
    with client.websocket_connect(WS) as conn:
        bridge = FakeBridge.spawned[-1]
        conn.send_text("\x1b[RESIZE:70000;70000]")
        conn.send_bytes(b"a")
        assert conn.receive_bytes() == b"echo:a"
        assert bridge.size == (plugin.MAX_COLS, plugin.MAX_ROWS)
        conn.send_text("\x1b[RESIZE:0;30]")
        conn.send_bytes(b"b")
        assert conn.receive_bytes() == b"echo:b"
        assert bridge.size == (plugin.MAX_COLS, plugin.MAX_ROWS)  # unchanged: a zero size is dropped
        assert bytes(bridge.written) == b"ab"  # resize escapes never reach the shell
    _wait_closed(bridge)


# --- Limits --------------------------------------------------------------------

def test_two_shell_limit(client, plugin, tmp_path):
    with client.websocket_connect(WS) as first, client.websocket_connect(WS) as second:
        first.send_bytes(b"1")
        assert first.receive_bytes() == b"echo:1"
        second.send_bytes(b"2")
        assert second.receive_bytes() == b"echo:2"
        assert len(plugin._ACTIVE) == 2
        with client.websocket_connect(WS) as third:
            assert "terminal limit reached" in third.receive_text()  # red banner, then close
            with pytest.raises(WebSocketDisconnect) as exc:
                third.receive_text()
            assert exc.value.code == plugin.CLOSE_BUSY
        assert len(FakeBridge.spawned) == 2  # the third never spawned a shell
    for b in FakeBridge.spawned:
        _wait_closed(b)
    denies = [e for e in _audit_lines(tmp_path) if e["event"] == "deny"]
    assert len(denies) == 1 and denies[0]["reason"] == "limit" and denies[0]["active"] == 2


def test_idle_timeout_closes_shell(client, plugin, monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, "IDLE_TIMEOUT_S", 0.3)
    with client.websocket_connect(WS) as conn:
        bridge = FakeBridge.spawned[-1]
        conn.send_bytes(b"k")
        assert conn.receive_bytes() == b"echo:k"
        with pytest.raises(WebSocketDisconnect) as exc:
            conn.receive_bytes()
        assert exc.value.code == plugin.CLOSE_IDLE
    _wait_closed(bridge)
    closes = [e for e in _audit_lines(tmp_path) if e["event"] == "close"]
    assert closes and closes[-1]["reason"] == "idle_timeout"


def test_resize_frames_do_not_reset_the_idle_timeout(client, plugin, monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, "IDLE_TIMEOUT_S", 0.5)
    with client.websocket_connect(WS) as conn:
        bridge = FakeBridge.spawned[-1]
        # A client that only ever resizes (window drag, layout tick) is still idle.
        deadline = time.monotonic() + 1.2
        while time.monotonic() < deadline and not bridge.closed:
            conn.send_text("\x1b[RESIZE:100;30]")
            time.sleep(0.1)
        _wait_closed(bridge, timeout=2.0)  # bounded: a regression hangs here, not forever
        assert bridge.size == (100, 30)  # the resizes were still applied
        with pytest.raises(WebSocketDisconnect) as exc:
            conn.receive_bytes()
        assert exc.value.code == plugin.CLOSE_IDLE
    closes = [e for e in _audit_lines(tmp_path) if e["event"] == "close"]
    assert closes and closes[-1]["reason"] == "idle_timeout"


def test_spawn_failure_closes_1011_without_slot_leak(client, plugin, monkeypatch, tmp_path):
    class Broken(FakeBridge):
        @classmethod
        def spawn(cls, *a, **kw):
            raise OSError("no pty")
    monkeypatch.setattr(plugin, "_core", lambda: _fake_core(bridge=Broken))
    with client.websocket_connect(WS) as conn:
        assert "could not start" in conn.receive_text()  # red banner, then close
        with pytest.raises(WebSocketDisconnect) as exc:
            conn.receive_text()
        assert exc.value.code == plugin.CLOSE_ERROR
    _wait_no_active(plugin)
    assert [e["event"] for e in _audit_lines(tmp_path)] == ["error"]


# --- Input bounds --------------------------------------------------------------

def test_input_frame_at_the_limit_is_forwarded(client, plugin):
    with client.websocket_connect(WS) as conn:
        bridge = FakeBridge.spawned[-1]
        frame = b"z" * plugin.MAX_INPUT_FRAME_BYTES
        conn.send_bytes(frame)
        assert conn.receive_bytes() == b"echo:" + frame
    _wait_closed(bridge)


def test_oversized_input_frame_closes_the_shell(client, plugin, tmp_path):
    with client.websocket_connect(WS) as conn:
        bridge = FakeBridge.spawned[-1]
        conn.send_bytes(b"x" * (plugin.MAX_INPUT_FRAME_BYTES + 1))
        with pytest.raises(WebSocketDisconnect) as exc:
            conn.receive_bytes()
        assert exc.value.code == plugin.CLOSE_TOO_BIG
    _wait_closed(bridge)
    assert bytes(bridge.written) == b""  # nothing of it reached the shell
    closes = [e for e in _audit_lines(tmp_path) if e["event"] == "close"]
    assert closes and closes[-1]["reason"] == "input_too_large"


def test_input_flood_closes_the_shell(client, plugin, monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, "MAX_INPUT_BYTES_PER_WINDOW", 1000)
    with client.websocket_connect(WS) as conn:
        bridge = FakeBridge.spawned[-1]
        for _ in range(10):
            conn.send_bytes(b"y" * 200)
        with pytest.raises(WebSocketDisconnect) as exc:
            while True:
                conn.receive_bytes()  # the echoes that made it, then the close
        assert exc.value.code == plugin.CLOSE_POLICY
    _wait_closed(bridge)
    assert len(bridge.written) == 1000  # five frames in the window, the sixth closed it
    closes = [e for e in _audit_lines(tmp_path) if e["event"] == "close"]
    assert closes and closes[-1]["reason"] == "input_flood"


# --- HTTP ----------------------------------------------------------------------

def test_health_reports_contract_and_limits(client, plugin, monkeypatch):
    body = client.get(PREFIX + "/health").json()
    assert body["ok"] is True and body["contract_ok"] is True and body["pty_available"] is True
    assert body["limit"] == 2 and body["active"] == 0 and body["idle_timeout_seconds"] == 1200
    assert body["allowed_users_configured"] is True
    monkeypatch.setattr(plugin, "_core", lambda: None)
    body = client.get(PREFIX + "/health").json()
    assert body["ok"] is False and body["contract_ok"] is False
