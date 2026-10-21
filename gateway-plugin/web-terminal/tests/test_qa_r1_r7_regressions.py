"""Independent, workspace-only review probes of the exact checked-out plugin.

No live service, accounts, network listener, real shell, or deployment changes.
Regression copy of QA card t_f2d15f30 probes (reviewed 80c03b2), adapted only to 0.2.1
interfaces (_reap(slot, bridge), version, async helpers). R1-R7 safety assertions must PASS.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import threading
import time
import types
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI
from starlette.datastructures import QueryParams
from starlette.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "dashboard/plugin_api.py"
PREFIX = "/api/plugins/web-terminal"
OWNER = {"provider": "test", "user_id": "owner"}
GUEST = {"provider": "test", "user_id": "guest"}


def load_plugin():
    name = "independent_web_terminal_probe"
    spec = importlib.util.spec_from_file_location(name, PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def config_text(user="owner"):
    return ("plugins:\n  enabled: [web-terminal]\n  entries:\n"
            "    web-terminal:\n      settings:\n"
            f"        allowed_users: ['test:{user}']\n")


class Bridge:
    spawned = []

    def __init__(self, **kwargs):
        self.size = (kwargs["cols"], kwargs["rows"])
        self.pid = 12345
        self.writes = []
        self.resizes = []
        self.closed = False
        self.spawned.append(self)

    @classmethod
    def spawn(cls, argv, **kwargs):
        return cls(**kwargs)

    @classmethod
    def is_available(cls):
        return True

    def read(self, timeout):
        time.sleep(min(timeout, 0.005))
        return None if self.closed else b""

    async def write(self, raw):
        self.writes.append(raw)
        return True

    def resize(self, cols, rows):
        self.size = (cols, rows)
        self.resizes.append(self.size)

    def close(self):
        self.closed = True

    def is_alive(self):
        return not self.closed


def fake_core(bridge=Bridge, identity=OWNER):
    def auth(ws):
        if identity is not None:
            ws._hermes_auth_identity = dict(identity)
        return None, "test"
    return types.SimpleNamespace(_ws_auth_reason=auth, _ws_request_is_allowed=lambda ws: True,
                                 PtyBridge=bridge, PtyUnavailableError=RuntimeError)


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    Bridge.spawned = []
    mod = load_plugin()
    monkeypatch.setattr(mod, "_core", lambda: fake_core())
    monkeypatch.setattr(mod, "_plugin_enabled", lambda: True)
    monkeypatch.setattr(mod, "_allowed_users", lambda: frozenset({"test:owner"}))
    monkeypatch.setattr(mod, "_shell_env", lambda: {"PATH": "/usr/bin:/bin", "TERM": "xterm"})
    mod.audit_events = []
    monkeypatch.setattr(mod, "_audit", lambda event, **fields: mod.audit_events.append({"event": event, **fields}))
    return mod


class Socket:
    """Scripted ASGI-like socket; no actual network or fragmentation/reassembly layer."""
    def __init__(self, messages=(), query="", clock=None):
        self.query_params = QueryParams(query)
        self.client = types.SimpleNamespace(host="testclient")
        self.messages = iter(messages)
        self.clock = clock
        self.accepted = False
        self.closes = []

    async def accept(self):
        self.accepted = True

    async def receive(self):
        try:
            item = next(self.messages)
        except StopIteration:
            return {"type": "websocket.disconnect", "code": 1000}
        if self.clock is not None:
            when, item = item
            self.clock.now = when
        if isinstance(item, dict):
            return item
        return {"type": "websocket.receive", "bytes": item}

    async def send_bytes(self, raw):
        pass

    async def send_text(self, text):
        pass

    async def close(self, code=1000, reason=""):
        self.closes.append(code)


def run_socket(mod, sock):
    asyncio.run(mod.terminal_ws(sock))


def test_broken_config_must_not_authorize_from_last_known_good(monkeypatch, tmp_path):
    from hermes_cli import config
    home = tmp_path / "synthetic-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    path = home / "config.yaml"
    path.write_text(config_text(), encoding="utf-8")
    mod = load_plugin()
    monkeypatch.setattr(mod, "_core", lambda: fake_core())
    monkeypatch.setattr(mod, "_audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod, "_shell_env", lambda: {"TERM": "xterm"})
    Bridge.spawned = []
    assert mod._plugin_enabled() and mod._user_allowed(OWNER)
    path.write_text("plugins: [\n", encoding="utf-8")
    kind = type(config.load_config_readonly()).__name__
    sock = Socket()
    run_socket(mod, sock)
    print("R1", json.dumps({"config_type": kind, "enabled": mod._plugin_enabled(),
                             "owner_allowed": mod._user_allowed(OWNER), "spawns": len(Bridge.spawned)}))
    assert not sock.accepted and Bridge.spawned == []


@pytest.fixture
def mounted(monkeypatch, tmp_path):
    # Use the REAL mount helper and real async scope dependency. Stub only external
    # secret-source hydration / scope construction, and resolve the synthetic profile.
    from hermes_cli import web_server_dashboard as dashboard
    from hermes_cli import web_server_profiles as profiles
    from hermes_cli import env_loader
    from agent import secret_scope
    from tui_gateway import launch_profile_policy
    from hermes_cli import plugin_isolation
    from hermes_constants import get_hermes_home

    home = tmp_path / "launch"
    foreign = tmp_path / "foreign"
    home.mkdir()
    foreign.mkdir()
    (home / "config.yaml").write_text(config_text("owner"), encoding="utf-8")
    (foreign / "config.yaml").write_text(config_text("guest"), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(profiles, "_resolve_profile_dir", lambda name: foreign)
    monkeypatch.setattr(env_loader, "hydrate_profile_secret_sources", lambda path: None)
    monkeypatch.setattr(secret_scope, "build_profile_secret_scope", lambda path: {})
    monkeypatch.setattr(secret_scope, "set_secret_scope", lambda *a, **kw: None)
    monkeypatch.setattr(secret_scope, "reset_secret_scope", lambda token: None)
    monkeypatch.setattr(launch_profile_policy, "launch_secret_scope", lambda path: {})
    monkeypatch.setattr(launch_profile_policy, "activate_multi_profile_hosting", lambda: None)
    monkeypatch.setattr(plugin_isolation, "isolation_mode", lambda: "in_process")
    app = FastAPI()

    @app.middleware("http")
    async def stamp(request, call_next):
        request.state.session = types.SimpleNamespace(**GUEST)
        return await call_next(request)

    descriptor = {"name": "web-terminal", "source": "user", "_dir": str(PLUGIN.parent),
                  "_api_file": PLUGIN.name}
    assert dashboard._mount_one_plugin_api(app, descriptor, {"web-terminal"}, set())
    mod = sys.modules["hermes_dashboard_plugin_web-terminal"]
    monkeypatch.setattr(mod, "_core", lambda: fake_core(identity=GUEST))
    monkeypatch.setattr(mod, "_audit", lambda *a, **kw: None)
    monkeypatch.setattr(mod, "_shell_env", lambda: {"TERM": "xterm"})
    observed_homes = []
    original = mod._allowed_users

    def allowed():
        observed_homes.append(get_hermes_home())
        return original()

    monkeypatch.setattr(mod, "_allowed_users", allowed)
    return app, mod, home, foreign, observed_homes


def test_shell_preflight_must_reject_foreign_profile(mounted):
    app, mod, home, foreign, observed = mounted
    with TestClient(app) as client:
        plain = client.get(PREFIX + "/shell")
        other = client.get(PREFIX + "/shell?profile=foreign")
    print("R2", json.dumps({"plain_status": plain.status_code, "foreign_status": other.status_code,
                             "foreign_config_used": observed[-1] == foreign,
                             "cwd_exposed": "cwd" in other.json()}))
    assert observed == [home]  # adapted for R2: foreign-scoped request refused before any allow-list read
    assert plain.status_code == 403
    assert other.status_code == 403


def test_mounted_ws_refuses_profile_before_owner_list_but_after_enabled_read(mounted, monkeypatch):
    from hermes_constants import get_hermes_home
    app, mod, home, foreign, observed = mounted
    enabled_homes = []
    original = mod._plugin_enabled

    def enabled():
        enabled_homes.append(get_hermes_home())
        return original()

    monkeypatch.setattr(mod, "_plugin_enabled", enabled)
    with TestClient(app) as client, pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(PREFIX + "/terminal?profile=foreign"):
            pass
    assert exc.value.code == 4403
    assert observed == []
    assert enabled_homes == []  # adapted for R2: refusal now precedes every policy read
    print("WS_PROFILE", json.dumps({"code": exc.value.code, "allowed_list_reads": len(observed),
                                    "foreign_enabled_read": bool(enabled_homes)}))


def test_rolling_window_must_reject_boundary_burst(plugin, monkeypatch):
    clock = types.SimpleNamespace(now=0.0)
    proxy = types.SimpleNamespace(time=lambda: clock.now)
    monkeypatch.setattr(plugin, "asyncio", types.SimpleNamespace(**{k: getattr(asyncio, k) for k in ("Event", "wait", "wait_for", "FIRST_COMPLETED", "TimeoutError", "Task")},  # adapted: current handler uses these
        get_running_loop=lambda: proxy, create_task=asyncio.create_task,
        to_thread=asyncio.to_thread, sleep=asyncio.sleep, CancelledError=asyncio.CancelledError))
    monkeypatch.setattr(plugin, "_reap", lambda slot, b: (b.close(), plugin._release_slot(slot["key"])))
    frame = b"x" * plugin.MAX_INPUT_FRAME_BYTES
    n = plugin.MAX_INPUT_BYTES_PER_WINDOW // len(frame)
    sock = Socket([(9.9, frame)] * n + [(10.1, frame)] * n, clock=clock)
    run_socket(plugin, sock)
    written = sum(len(b) for b in Bridge.spawned[-1].writes)
    print("R3", json.dumps({"bytes_in_0_2_seconds": written, "policy_close": 1008 in sock.closes,
                             "limit": plugin.MAX_INPUT_BYTES_PER_WINDOW}))
    assert written <= plugin.MAX_INPUT_BYTES_PER_WINDOW
    assert 1008 in sock.closes


def test_frame_splitting_does_not_evade_same_window(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "MAX_INPUT_BYTES_PER_WINDOW", 1000)
    monkeypatch.setattr(plugin, "_reap", lambda slot, b: (b.close(), plugin._release_slot(slot["key"])))
    sock = Socket([b"x" * 100] * 11)
    run_socket(plugin, sock)
    assert sum(map(len, Bridge.spawned[-1].writes)) == 1000
    assert 1008 in sock.closes


@pytest.mark.parametrize("raw", [b"x" * (64 * 1024 + 1)])
def test_frame_size_enforced_before_bridge_write(plugin, monkeypatch, raw):
    monkeypatch.setattr(plugin, "_reap", lambda slot, b: (b.close(), plugin._release_slot(slot["key"])))
    sock = Socket([raw])
    run_socket(plugin, sock)
    assert 1009 in sock.closes
    assert not Bridge.spawned[-1].writes


def test_multiple_resize_controls_are_not_keyboard_input(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_reap", lambda slot, b: (b.close(), plugin._release_slot(slot["key"])))
    frame = b"\x1b[RESIZE:100;30]\x1b[RESIZE:120;40]"
    sock = Socket([frame])
    run_socket(plugin, sock)
    bridge = Bridge.spawned[-1]
    print("R4", json.dumps({"resize_calls": len(bridge.resizes), "written_bytes": sum(map(len, bridge.writes)),
                             "last_size": bridge.size}))
    assert bridge.writes == []
    assert bridge.size == (120, 40)


def test_resize_plus_real_byte_is_input_by_design(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_reap", lambda slot, b: (b.close(), plugin._release_slot(slot["key"])))
    run_socket(plugin, Socket([b"\x1b[RESIZE:120;40]x"]))
    bridge = Bridge.spawned[-1]
    assert bridge.size == (120, 40) and bridge.writes == [b"x"]


def test_reaping_shells_still_count_towards_cap(plugin, monkeypatch):
    release = threading.Event()
    started = threading.Event()
    threads = []

    class SlowClose(Bridge):
        def close(self):
            started.set()
            release.wait(timeout=3.0)
            self.closed = True

    monkeypatch.setattr(plugin, "_core", lambda: fake_core(bridge=SlowClose))
    original = plugin._reap

    def reap(slot, bridge):
        t = original(slot, bridge)
        threads.append(t)
        return t

    monkeypatch.setattr(plugin, "_reap", reap)
    try:
        for _ in range(plugin.MAX_TERMINALS + 1):
            run_socket(plugin, Socket())
        assert started.wait(timeout=1.0)
        live = sum(not b.closed for b in Bridge.spawned)
        print("R5", json.dumps({"live_or_reaping_bridges": live, "active_slots": len(plugin._ACTIVE),
                                 "cap": plugin.MAX_TERMINALS}))
        assert live <= plugin.MAX_TERMINALS
    finally:
        release.set()
        for t in threads:
            t.join(timeout=2.0)
        assert all(not t.is_alive() for t in threads)


@pytest.mark.parametrize("shape", ["async", "tuple"])
def test_origin_contract_drift_must_fail_closed(plugin, monkeypatch, shape):
    core = fake_core()

    async def deny(ws):
        return False

    core._ws_request_is_allowed = deny if shape == "async" else lambda ws: (False, "origin_mismatch")
    monkeypatch.setitem(sys.modules, plugin._CORE_MODULE, core)
    # Restore the exact contract validator, not the fixture's lambda.
    original = load_plugin()
    assert original._core() in (core, None)  # adapted: current contract check may refuse an async gate at resolution
    monkeypatch.setattr(plugin, "_core", original._core)
    monkeypatch.setattr(plugin, "_reap", lambda slot, b: (b.close(), plugin._release_slot(slot["key"])))
    sock = Socket()
    run_socket(plugin, sock)
    print("R6", json.dumps({"shape": shape, "accepted": sock.accepted, "spawns": len(Bridge.spawned)}))
    assert not sock.accepted and Bridge.spawned == []


def test_output_backpressure_must_not_disable_idle_timeout(plugin, monkeypatch):
    class OutputBridge(Bridge):
        def read(self, timeout):
            return b"output" if not self.closed else None

    class StalledSocket(Socket):
        def __init__(self):
            super().__init__()
            self.sending = asyncio.Event()
            self.hold = asyncio.Event()

        async def receive(self):
            await self.hold.wait()
            return {"type": "websocket.disconnect"}

        async def send_bytes(self, raw):
            self.sending.set()
            await self.hold.wait()

    monkeypatch.setattr(plugin, "_core", lambda: fake_core(bridge=OutputBridge))
    monkeypatch.setattr(plugin, "_reap", lambda slot, b: (b.close(), plugin._release_slot(slot["key"])))
    monkeypatch.setattr(plugin, "IDLE_TIMEOUT_S", 0.05)

    async def exercise():
        sock = StalledSocket()
        task = asyncio.create_task(plugin.terminal_ws(sock))
        try:
            await asyncio.wait_for(sock.sending.wait(), timeout=1.0)
            await asyncio.sleep(0.15)
            observed = {"idle_timeout_seconds": plugin.IDLE_TIMEOUT_S,
                        "waited_seconds": 0.15, "idle_closed": 4409 in sock.closes,
                        "active_slots": len(plugin._ACTIVE)}
            print("R7", json.dumps(observed))
            assert 4409 in sock.closes
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            assert not plugin._ACTIVE

    asyncio.run(exercise())


@pytest.mark.parametrize("value", [False, None])
def test_false_or_missing_origin_denies(plugin, monkeypatch, value):
    core = fake_core()
    core._ws_request_is_allowed = lambda ws: value
    monkeypatch.setattr(plugin, "_core", lambda: core)
    sock = Socket()
    run_socket(plugin, sock)
    assert not sock.accepted and 4403 in sock.closes and not Bridge.spawned


@pytest.mark.parametrize("mode,user,expect_allowed", [
    ("ticket", "owner", True), ("ticket", "guest", False),
    ("internal", "owner", False), ("loopback", "owner", False),
    ("session", "owner", True), ("session", "guest", False), ("invalid-session", "owner", False),
])
def test_real_core_identity_paths(plugin, monkeypatch, mode, user, expect_allowed):
    from hermes_cli import web_server_chat as core
    from hermes_cli.dashboard_auth import audit, ws_tickets, middleware
    server = types.ModuleType("hermes_cli.web_server")
    server.app = FastAPI()
    server.app.state.auth_required = mode != "loopback"
    server._SESSION_TOKEN = "synthetic-session-only"
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server", server)
    monkeypatch.setattr(audit, "audit_log", lambda *a, **kw: None)
    # No real credentials: only tokens minted in this test process or a stub provider.
    if mode == "ticket":
        query = {"ticket": ws_tickets.mint_ticket(user_id=user, provider="test")}
    elif mode == "internal":
        query = {"internal": ws_tickets.internal_ws_credential()}
    elif mode == "loopback":
        query = {"token": server._SESSION_TOKEN}
    else:
        session = None if mode == "invalid-session" else types.SimpleNamespace(user_id=user, provider="test")
        monkeypatch.setattr(middleware, "_verify_access_token", lambda *a, **kw: session)
        query = {"token": "synthetic-session-only"}
    query.update(user_id="owner", provider="test")
    ws = WebSocket({"type": "websocket", "path": PREFIX + "/terminal", "scheme": "ws",
                    "query_string": urlencode(query).encode(), "headers": [], "client": ("testclient", 1),
                    "server": ("testserver", 80), "root_path": "", "subprotocols": []},
                   receive=lambda: None, send=lambda value: None)
    reason, credential_kind = core._ws_auth_reason(ws)
    ident = plugin._identity(ws)
    allowed = reason is None and plugin._user_allowed(ident)
    assert allowed is expect_allowed
    if mode == "loopback":
        assert ident == {}
    elif mode == "internal":
        assert ident == {"provider": "server-internal", "user_id": "server-internal"}
    print("REAL_AUTH", json.dumps({"mode": mode, "test_identity": user, "kind": credential_kind,
                                    "stamp_present": bool(ident), "allowed": allowed}))


def test_5000_digit_resize_ignored_and_all_spawn_dimensions_safe(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_reap", lambda slot, b: (b.close(), plugin._release_slot(slot["key"])))
    run_socket(plugin, Socket([b"\x1b[RESIZE:" + b"9" * 5000 + b";24]"],
                              query=urlencode({"cols": "9" * 5000, "rows": "70000"})))
    bridge = Bridge.spawned[-1]
    assert bridge.size == (80, 1000) and not bridge.resizes and not bridge.writes


def test_oversized_unicode_text_counts_encoded_bytes(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_reap", lambda slot, b: (b.close(), plugin._release_slot(slot["key"])))
    sock = Socket([{"type": "websocket.receive", "text": "\u20ac" * 22000}])
    run_socket(plugin, sock)
    assert 1009 in sock.closes and not Bridge.spawned[-1].writes


def test_wire_fragmentation_does_not_evade_message_limit(plugin, monkeypatch):
    from websockets.frames import Frame, OP_BINARY, OP_CONT
    from websockets.legacy.protocol import WebSocketCommonProtocol
    frames = iter([Frame(OP_BINARY, b"x" * 32768, fin=False), Frame(OP_CONT, b"x" * 32769, fin=True)])

    async def read_data_frame(max_size):
        return next(frames)

    proto = types.SimpleNamespace(read_data_frame=read_data_frame, max_size=384 * 1024 * 1024)
    raw = asyncio.run(WebSocketCommonProtocol.read_message(proto))
    assert len(raw) == 65537
    monkeypatch.setattr(plugin, "_reap", lambda slot, b: (b.close(), plugin._release_slot(slot["key"])))
    sock = Socket([raw])
    run_socket(plugin, sock)
    assert 1009 in sock.closes and not Bridge.spawned[-1].writes


def test_missing_environment_scrub_does_not_spawn(plugin, monkeypatch):
    def fail_scrub():
        raise ImportError("synthetic scrub unavailable")

    monkeypatch.setattr(plugin, "_shell_env", fail_scrub)
    sock = Socket()
    run_socket(plugin, sock)
    assert 1011 in sock.closes and not Bridge.spawned and not plugin._ACTIVE


def test_origin_exception_denies(plugin, monkeypatch):
    core = fake_core()

    def failure(ws):
        raise RuntimeError("synthetic gate failure")

    core._ws_request_is_allowed = failure
    monkeypatch.setattr(plugin, "_core", lambda: core)
    sock = Socket()
    run_socket(plugin, sock)
    assert not sock.accepted and 4403 in sock.closes and not Bridge.spawned


def test_manifest_schema_and_versions_agree():
    import yaml
    from hermes_cli.plugins_manifest import validate_config_schema
    manifest = yaml.safe_load((PLUGIN.parents[1] / "plugin.yaml").read_text())
    dashboard = json.loads((PLUGIN.parent / "manifest.json").read_text())
    mod = load_plugin()
    assert manifest["version"] == dashboard["version"] == mod.PLUGIN_VERSION == "0.2.1"
    schema = manifest["config_schema"]
    assert schema["allowed_users"]["type"] == "list" and schema["allowed_users"]["required"] is True
    assert validate_config_schema("web-terminal", schema, {"allowed_users": ["test:owner"]}) == []
    assert len(validate_config_schema("web-terminal", schema, {})) == 1
