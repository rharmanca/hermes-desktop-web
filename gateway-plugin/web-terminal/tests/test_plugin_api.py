"""Tests for the web-terminal dashboard plugin (gateway-plugin/web-terminal/dashboard/plugin_api.py).

Run with the Hermes venv so the real ``hermes_cli`` is importable for the contract tests
(``PYTHONPATH`` = the hermes-agent checkout, as the dashboard itself puts that root on ``sys.path``):

    cd <repo> && HERMES_HOME=~/.hermes PYTHONPATH=<hermes-agent> \
        <hermes-agent>/.venv/bin/python -m pytest gateway-plugin/web-terminal/tests -q -o addopts=""
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import queue
import sys
import threading
import time
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.datastructures import QueryParams
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

PLUGIN_FILE = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
PREFIX = "/api/plugins/web-terminal"
WS = PREFIX + "/terminal"

# Identities the fake core gate stamps on the socket (the way the real gate does), and the
# allow-list the fixture installs: only ``test:owner`` may open a shell.
_OWNER = {"user_id": "owner", "provider": "test"}
_GUEST = {"user_id": "guest", "provider": "test"}
_INTERNAL = {"user_id": "server-internal", "provider": "server-internal"}


def _load_plugin(name: str = "hermes_dashboard_plugin_web_terminal_test"):
    spec = importlib.util.spec_from_file_location(name, PLUGIN_FILE)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _config_text(user: str = "owner") -> str:
    return ("plugins:\n  enabled: [web-terminal]\n  entries:\n    web-terminal:\n      settings:\n"
            f"        allowed_users: ['test:{user}']\n")


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

    _pgid = None

    @property
    def _proc(self):
        # Minimal stand-in for the core's private ptyprocess handle (cleanup confirmation).
        return types.SimpleNamespace(pid=self.pid, isalive=lambda: not self.closed)

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


def _fake_core(auth=(None, "test"), allowed=True, identity=_OWNER, bridge=None, origin=None):
    return types.SimpleNamespace(
        _ws_auth_reason=_stamping_auth(identity, auth),
        _ws_request_is_allowed=origin or (lambda ws: allowed),
        PtyBridge=bridge or FakeBridge,
        PtyUnavailableError=RuntimeError,
    )


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    """Plugin module wired to a fake core gate + fake PTY, audit log under a temp HERMES_HOME,
    ``test:owner`` as the only allowed user."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
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


def _wait_active(plugin, n: int, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while len(plugin._ACTIVE) != n and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(plugin._ACTIVE) == n, f"expected {n} active slot(s), got {len(plugin._ACTIVE)}"


def _wait_no_active(plugin, timeout: float = 3.0) -> None:
    _wait_active(plugin, 0, timeout)


class ScriptedSocket:
    """Minimal ASGI-style WebSocket stand-in for driving ``terminal_ws`` directly: a browser that
    never sends input and (optionally) stops reading output. Mirrors uvicorn: once the app has
    sent ``websocket.close``, the next ``receive()`` returns a disconnect."""

    def __init__(self, query: str = "", stall_sends: bool = False):
        self.query_params = QueryParams(query)
        self.client = types.SimpleNamespace(host="testclient")
        self.stall_sends = stall_sends
        self.closed = asyncio.Event()
        self.sending = asyncio.Event()
        self.closes: list[tuple[int, str]] = []
        self.sent = 0

    async def accept(self) -> None:
        return None

    async def receive(self):
        await self.closed.wait()
        return {"type": "websocket.disconnect", "code": 1005}

    async def send_bytes(self, raw: bytes) -> None:
        self.sending.set()
        if self.stall_sends:
            await self.closed.wait()  # a browser that stopped reading: the send never completes
            raise RuntimeError("socket closed")
        self.sent += len(raw)

    async def send_text(self, text: str) -> None:
        return None

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closes.append((code, reason))
        self.closed.set()


class OutputBridge(FakeBridge):
    """A shell that prints continuously."""

    def read(self, timeout: float):
        if self.closed:
            return None
        time.sleep(min(timeout, 0.005))
        return b"output"


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
    monkeypatch.setitem(sys.modules, "hermes_cli.config", None)  # import -> ImportError
    assert mod._plugin_enabled() is False
    assert mod._allowed_users() == frozenset()


def test_contract_fails_closed_when_core_import_breaks(monkeypatch):
    mod = _load_plugin("hermes_dashboard_plugin_web_terminal_contract2")
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_chat", None)  # import -> ImportError
    assert mod._core() is None


def test_contract_fails_closed_when_gate_renamed_or_made_async(monkeypatch):
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
    stub.PtyBridge = FakeBridge
    assert mod._core() is stub  # whole contract present again

    async def async_origin(ws):  # an async gate returns a (truthy) coroutine object: contract drift
        return False
    stub._ws_request_is_allowed = async_origin
    assert mod._core() is None
    stub._ws_request_is_allowed = lambda ws: True

    async def async_auth(ws):
        return None, "x"
    stub._ws_auth_reason = async_auth
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


# --- Policy source: readable launch-home config or nothing (R1, R2) ----------------

def test_policy_denies_on_broken_config_instead_of_serving_last_known_good(monkeypatch, tmp_path):
    """R1: after a parse failure Hermes serves a *last-known-good* ``FailedConfigRead``. That is
    stale permission data: the plugin must treat it as unreadable -> disabled, nobody allowed,
    dial refused — even though the same config authorised the owner a moment earlier."""
    from hermes_cli.config_read_errors import FailedConfigRead
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    (home / "config.yaml").write_text(_config_text("owner"), encoding="utf-8")
    mod = _load_plugin("hermes_dashboard_plugin_web_terminal_r1")
    mod._ACTIVE.clear()
    FakeBridge.spawned.clear()
    monkeypatch.setattr(mod, "_core", lambda: _fake_core())
    assert mod._plugin_enabled() is True and mod._user_allowed(_OWNER) is True

    (home / "config.yaml").write_text("plugins: [\n", encoding="utf-8")  # broken YAML
    from hermes_cli.config import load_config_readonly
    assert isinstance(load_config_readonly(), FailedConfigRead)  # the core's fallback is in play
    assert mod._policy_config() is None
    assert mod._plugin_enabled() is False and mod._user_allowed(_OWNER) is False

    app = FastAPI()
    app.include_router(mod.router, prefix=PREFIX)
    with TestClient(app) as c:
        assert c.get(PREFIX + "/health").json()["policy_readable"] is False
        with pytest.raises(WebSocketDisconnect) as exc:
            with c.websocket_connect(WS):
                pass
        assert exc.value.code == mod.CLOSE_DISABLED
    assert FakeBridge.spawned == []

    (home / "config.yaml").write_text(_config_text("owner"), encoding="utf-8")  # repaired: back on
    assert mod._plugin_enabled() is True and mod._user_allowed(_OWNER) is True


def test_policy_is_never_read_under_another_profiles_home(monkeypatch, tmp_path):
    """R2: a ``?profile=`` request runs inside a context-local HERMES_HOME override for that
    profile. Policy comes from the launch home only, so under any foreign override the config
    counts as unreadable: disabled, nobody."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    home, foreign = tmp_path / "home", tmp_path / "foreign"
    home.mkdir()
    foreign.mkdir()
    (home / "config.yaml").write_text(_config_text("owner"), encoding="utf-8")
    (foreign / "config.yaml").write_text(_config_text("guest"), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    mod = _load_plugin("hermes_dashboard_plugin_web_terminal_r2")
    assert mod._allowed_users() == frozenset({"test:owner"})
    token = set_hermes_home_override(str(foreign))
    try:
        assert mod._policy_config() is None
        assert mod._plugin_enabled() is False
        assert mod._allowed_users() == frozenset()
        assert mod._user_allowed(_GUEST) is False and mod._user_allowed(_OWNER) is False
    finally:
        reset_hermes_home_override(token)
    token = set_hermes_home_override(str(home))  # an override naming the launch home itself is fine
    try:
        assert mod._allowed_users() == frozenset({"test:owner"})
    finally:
        reset_hermes_home_override(token)


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


@pytest.fixture
def mounted(monkeypatch, tmp_path):
    """The plugin mounted through the dashboard's REAL mount helper, i.e. behind the real
    ``?profile=`` scope dependency, against two synthetic homes. Only external secret-source
    hydration / scope construction is stubbed (nothing private is read). If a Hermes update
    changes these internals this fixture fails loudly: re-verify the profile-scope guarantee."""
    from agent import secret_scope
    from hermes_cli import env_loader, plugin_isolation, web_server_dashboard as dashboard
    from hermes_cli import web_server_profiles as profiles
    from tui_gateway import launch_profile_policy

    home, foreign = tmp_path / "launch", tmp_path / "foreign"
    home.mkdir()
    foreign.mkdir()
    (home / "config.yaml").write_text(_config_text("owner"), encoding="utf-8")
    (foreign / "config.yaml").write_text(_config_text("guest"), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.setattr(profiles, "_resolve_profile_dir", lambda name: foreign)
    monkeypatch.setattr(env_loader, "hydrate_profile_secret_sources", lambda path: None)
    monkeypatch.setattr(secret_scope, "build_profile_secret_scope", lambda path: {})
    monkeypatch.setattr(secret_scope, "set_secret_scope", lambda *a, **kw: None)
    monkeypatch.setattr(secret_scope, "reset_secret_scope", lambda token: None)
    monkeypatch.setattr(launch_profile_policy, "launch_secret_scope", lambda path: {})
    monkeypatch.setattr(launch_profile_policy, "activate_multi_profile_hosting", lambda: None)
    monkeypatch.setattr(plugin_isolation, "isolation_mode", lambda: "in_process")
    module_name = "hermes_dashboard_plugin_web-terminal"
    monkeypatch.delitem(sys.modules, module_name, raising=False)

    app = FastAPI()

    @app.middleware("http")
    async def _stamp_session(request, call_next):
        user = request.headers.get("x-test-user")
        request.state.session = types.SimpleNamespace(user_id=user, provider="test") if user else None
        return await call_next(request)

    descriptor = {"name": "web-terminal", "source": "user", "_dir": str(PLUGIN_FILE.parent),
                  "_api_file": PLUGIN_FILE.name}
    assert dashboard._mount_one_plugin_api(app, descriptor, {"web-terminal"}, set())
    mod = sys.modules[module_name]
    mod._ACTIVE.clear()
    FakeBridge.spawned.clear()
    monkeypatch.setattr(mod, "_core", lambda: _fake_core(identity=_GUEST))
    monkeypatch.setattr(mod, "_audit", lambda *a, **kw: None)
    reads: list[Path] = []
    original = mod._policy_config

    def recording_policy_config():
        from hermes_constants import get_hermes_home
        reads.append(get_hermes_home())
        return original()

    monkeypatch.setattr(mod, "_policy_config", recording_policy_config)
    return app, mod, home, foreign, reads


def test_real_mount_http_preflight_refuses_foreign_profile(mounted):
    """R2 (HTTP): under the real mount, ``/shell?profile=foreign`` must not consult the foreign
    allow-list — the guest it names stays out — and the launch-home owner still gets through."""
    app, mod, home, foreign, reads = mounted
    with TestClient(app) as c:
        assert c.get(PREFIX + "/shell", headers={"x-test-user": "guest"}).status_code == 403
        assert c.get(PREFIX + "/shell?profile=foreign", headers={"x-test-user": "guest"}).status_code == 403
        assert c.get(PREFIX + "/health?profile=foreign").status_code == 403
        ok = c.get(PREFIX + "/shell", headers={"x-test-user": "owner"})
        assert ok.status_code == 200 and ok.json()["cwd"]
    assert foreign not in reads  # policy was never evaluated under the foreign home
    assert reads and all(p == home for p in reads)


def test_real_mount_ws_refuses_foreign_profile_before_any_policy_read(mounted):
    app, mod, home, foreign, reads = mounted
    with TestClient(app) as c, pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect(WS + "?profile=foreign"):
            pass
    assert exc.value.code == mod.CLOSE_ORIGIN
    assert reads == []  # refused before enablement or allow-list were consulted
    assert FakeBridge.spawned == []


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
    monkeypatch.setattr(plugin, "_core", lambda: _fake_core(origin=boom))
    _expect_refused(client, plugin.CLOSE_ORIGIN)
    monkeypatch.setattr(plugin, "_core", lambda: types.SimpleNamespace(
        _ws_auth_reason=boom, _ws_request_is_allowed=lambda ws: True,
        PtyBridge=FakeBridge, PtyUnavailableError=RuntimeError))
    _expect_refused(client, plugin.CLOSE_AUTH)
    # A gate that now returns a bare bool / a 3-tuple instead of (reason, cred) must also refuse.
    for shape in (True, (None, "x", "extra"), None):
        monkeypatch.setattr(plugin, "_core", lambda s=shape: types.SimpleNamespace(
            _ws_auth_reason=lambda ws: s, _ws_request_is_allowed=lambda ws: True,
            PtyBridge=FakeBridge, PtyUnavailableError=RuntimeError))
        _expect_refused(client, plugin.CLOSE_AUTH)
    assert FakeBridge.spawned == []


def test_origin_gate_must_return_exactly_true(client, plugin, monkeypatch):
    """R6: a nonempty tuple, an int or a coroutine object are all truthy; none of them is
    "allowed". Only ``True`` is."""
    for result in ((False, "origin_mismatch"), (True, "ok"), 1, "yes", None, False):
        monkeypatch.setattr(plugin, "_core", lambda r=result: _fake_core(origin=lambda ws: r))
        _expect_refused(client, plugin.CLOSE_ORIGIN)

    async def async_gate(ws):
        return False
    # An async gate fails the contract check itself (-> CLOSE_AUTH, "core_contract") when it
    # arrives via the real resolver; and if it slipped past, the coroutine result is refused.
    monkeypatch.setattr(plugin, "_core", lambda: _fake_core(origin=async_gate))
    _expect_refused(client, plugin.CLOSE_ORIGIN)
    assert FakeBridge.spawned == []


def test_refuses_bad_origin(client, plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_core", lambda: _fake_core(allowed=False))
    _expect_refused(client, plugin.CLOSE_ORIGIN)
    assert FakeBridge.spawned == []


def test_refuses_profile_routing_before_any_policy_read(client, plugin, monkeypatch, tmp_path):
    reads: list[str] = []
    monkeypatch.setattr(plugin, "_plugin_enabled", lambda: reads.append("enabled") or True)
    monkeypatch.setattr(plugin, "_allowed_users", lambda: reads.append("allowed") or frozenset({"test:owner"}))
    _expect_refused(client, plugin.CLOSE_ORIGIN, WS + "?profile=work-primary")
    assert reads == [] and FakeBridge.spawned == []
    assert [e["reason"] for e in _audit_lines(tmp_path) if e["event"] == "deny"] == ["profile_param"]
    assert client.get(PREFIX + "/shell?profile=work-primary").status_code == 403
    assert client.get(PREFIX + "/health?profile=work-primary").status_code == 403
    assert reads == []


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


def test_refuses_the_process_internal_credential_even_if_listed(client, plugin, monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, "_allowed_users", lambda: frozenset({"server-internal:server-internal"}))
    monkeypatch.setattr(plugin, "_core", lambda: _fake_core(auth=(None, "internal"), identity=_INTERNAL))
    _expect_refused(client, plugin.CLOSE_FORBIDDEN)
    assert FakeBridge.spawned == []
    assert [e["reason"] for e in _audit_lines(tmp_path) if e["event"] == "deny"] == ["internal_credential"]


def test_identity_is_the_server_stamp_never_a_client_parameter(client, plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_core", lambda: _fake_core(identity=_GUEST))
    _expect_refused(client, plugin.CLOSE_FORBIDDEN, WS + "?user_id=owner&provider=test")
    assert FakeBridge.spawned == []


def test_refuses_everyone_until_allowed_users_is_configured(client, plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_allowed_users", lambda: frozenset())
    assert client.get(PREFIX + "/health").json()["allowed_users_configured"] is False
    _expect_refused(client, plugin.CLOSE_FORBIDDEN)  # even the would-be owner
    assert FakeBridge.spawned == []


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


def test_every_resize_control_in_a_frame_is_consumed(client, plugin):
    """R4: two controls in one message -> both consumed, the last valid size applied, nothing
    of either reaches the shell."""
    with client.websocket_connect(WS) as conn:
        bridge = FakeBridge.spawned[-1]
        conn.send_bytes(b"\x1b[RESIZE:100;30]\x1b[RESIZE:120;40]")
        conn.send_bytes(b"\x1b[RESIZE:0;0]x\x1b[RESIZE:90;20]")  # mixed frame: 'x' is typing
        assert conn.receive_bytes() == b"echo:x"
        assert bridge.size == (90, 20)
        assert bytes(bridge.written) == b"x"
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
    _wait_no_active(plugin)
    denies = [e for e in _audit_lines(tmp_path) if e["event"] == "deny"]
    assert len(denies) == 1 and denies[0]["reason"] == "limit" and denies[0]["active"] == 2


def test_shells_still_closing_count_towards_the_cap(client, plugin, monkeypatch, tmp_path):
    """R5: the slot is held until the PTY close sequence has finished, so a reconnect loop
    cannot stack live + exiting shells past MAX_TERMINALS."""
    release, started = threading.Event(), threading.Event()

    class SlowClose(FakeBridge):
        def close(self):
            started.set()
            release.wait(timeout=5.0)
            super().close()

    monkeypatch.setattr(plugin, "_core", lambda: _fake_core(bridge=SlowClose))
    with client.websocket_connect(WS) as first:
        first.send_bytes(b"1")
        assert first.receive_bytes() == b"echo:1"
    assert started.wait(timeout=2.0)  # first's reaper is now blocked inside close()
    assert len(plugin._ACTIVE) == 1  # ... and its slot is still held
    with client.websocket_connect(WS) as second:
        second.send_bytes(b"2")
        assert second.receive_bytes() == b"echo:2"
        assert len(plugin._ACTIVE) == 2
        with client.websocket_connect(WS) as third:  # 1 closing + 1 live = cap reached
            assert "terminal limit reached" in third.receive_text()
            with pytest.raises(WebSocketDisconnect) as exc:
                third.receive_text()
            assert exc.value.code == plugin.CLOSE_BUSY
        live_or_closing = sum(not b.closed for b in FakeBridge.spawned)
        assert live_or_closing == 2 and len(FakeBridge.spawned) == 2
        release.set()  # first's close completes -> its slot is released exactly then
        _wait_active(plugin, 1)
        with client.websocket_connect(WS) as fourth:  # admitted now
            fourth.send_bytes(b"4")
            assert fourth.receive_bytes() == b"echo:4"
    for b in FakeBridge.spawned:
        _wait_closed(b)
    _wait_no_active(plugin)
    denies = [e for e in _audit_lines(tmp_path) if e["event"] == "deny"]
    assert [d["reason"] for d in denies] == ["limit"] and denies[0]["active"] == 2


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
        # A client that only ever resizes (window drag, layout tick, coalesced controls) is idle.
        deadline = time.monotonic() + 1.2
        while time.monotonic() < deadline and not bridge.closed:
            conn.send_text("\x1b[RESIZE:100;30]\x1b[RESIZE:100;31]")
            time.sleep(0.1)
        _wait_closed(bridge, timeout=2.0)  # bounded: a regression hangs here, not forever
        assert bridge.size == (100, 31)  # the resizes were still applied
        assert bytes(bridge.written) == b""
        with pytest.raises(WebSocketDisconnect) as exc:
            conn.receive_bytes()
        assert exc.value.code == plugin.CLOSE_IDLE
    closes = [e for e in _audit_lines(tmp_path) if e["event"] == "close"]
    assert closes and closes[-1]["reason"] == "idle_timeout"


def test_idle_watchdog_runs_while_output_send_is_stalled(plugin, monkeypatch, tmp_path):
    """R7: a browser that stops reading blocks the PTY->WS pump inside send_bytes(); the idle
    clock must still fire (own task) and the slot must be released."""
    monkeypatch.setattr(plugin, "_core", lambda: _fake_core(bridge=OutputBridge))
    monkeypatch.setattr(plugin, "IDLE_TIMEOUT_S", 0.05)

    async def exercise():
        sock = ScriptedSocket(stall_sends=True)
        task = asyncio.create_task(plugin.terminal_ws(sock))
        await asyncio.wait_for(sock.sending.wait(), timeout=2.0)  # pump is now stuck in send_bytes
        await asyncio.wait_for(sock.closed.wait(), timeout=1.0)  # ... and the watchdog still closed it
        await asyncio.wait_for(task, timeout=2.0)
        return sock

    sock = asyncio.run(exercise())
    assert [c for c, _ in sock.closes][0] == plugin.CLOSE_IDLE
    _wait_closed(FakeBridge.spawned[-1])
    _wait_no_active(plugin)
    closes = [e for e in _audit_lines(tmp_path) if e["event"] == "close"]
    assert closes and closes[-1]["reason"] == "idle_timeout"


def test_handler_finishes_even_when_the_peer_never_answers(plugin, monkeypatch, tmp_path):
    """R7 (worst case): the peer neither reads output, nor completes the close handshake, nor
    ever sends a disconnect. The shell must still be reaped and the handler must still return
    (the server closes the transport when it does) — nothing may wait on that peer forever."""
    never = asyncio.Event()

    class HungSocket(ScriptedSocket):
        async def receive(self):
            await never.wait()

        async def send_bytes(self, raw: bytes) -> None:
            self.sending.set()
            await never.wait()

        async def close(self, code: int = 1000, reason: str = "") -> None:
            self.closes.append((code, reason))
            await never.wait()  # close handshake never completes

    monkeypatch.setattr(plugin, "_core", lambda: _fake_core(bridge=OutputBridge))
    monkeypatch.setattr(plugin, "IDLE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(plugin, "_CLOSE_TIMEOUT_S", 0.05)

    async def exercise():
        sock = HungSocket()
        task = asyncio.create_task(plugin.terminal_ws(sock))
        await asyncio.wait_for(sock.sending.wait(), timeout=2.0)
        await asyncio.wait_for(task, timeout=3.0)  # returns on its own: idle -> bounded close -> teardown
        return sock

    sock = asyncio.run(exercise())
    assert [c for c, _ in sock.closes][0] == plugin.CLOSE_IDLE
    _wait_closed(FakeBridge.spawned[-1])
    _wait_no_active(plugin)
    closes = [e for e in _audit_lines(tmp_path) if e["event"] == "close"]
    assert closes and closes[-1]["reason"] == "idle_timeout"


def test_stalled_output_send_ends_the_shell(plugin, monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, "_core", lambda: _fake_core(bridge=OutputBridge))
    monkeypatch.setattr(plugin, "OUTPUT_STALL_TIMEOUT_S", 0.05)

    async def exercise():
        sock = ScriptedSocket(stall_sends=True)
        task = asyncio.create_task(plugin.terminal_ws(sock))
        await asyncio.wait_for(sock.closed.wait(), timeout=2.0)
        await asyncio.wait_for(task, timeout=2.0)
        return sock

    sock = asyncio.run(exercise())
    assert [c for c, _ in sock.closes][0] == plugin.CLOSE_BUSY
    _wait_closed(FakeBridge.spawned[-1])
    _wait_no_active(plugin)
    closes = [e for e in _audit_lines(tmp_path) if e["event"] == "close"]
    assert closes and closes[-1]["reason"] == "output_stalled"


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

def test_input_budget_is_a_sliding_window(plugin):
    """R3: a burst straddling a bucket boundary is still one window's worth."""
    b = plugin._InputBudget(10.0, 1000)
    assert b.add(9.9, 1000) is False
    assert b.add(10.1, 1) is True  # 1001 bytes within 0.2 s: over, boundary or not
    b = plugin._InputBudget(10.0, 1000)
    assert b.add(0.0, 1000) is False
    assert b.add(9.5, 1) is True  # still inside the window
    b = plugin._InputBudget(10.0, 1000)
    assert b.add(0.0, 1000) is False
    assert b.add(9.999, 1) is True  # every byte counts for the full window, never early
    b = plugin._InputBudget(10.0, 1000)
    assert b.add(0.0, 1000) is False
    assert b.add(10.05, 1000) is False  # whole 50 ms quantum now window-old: a fresh window
    b = plugin._InputBudget(10.0, 1000)
    for t in range(0, 10):  # 100 B/s sustained is fine ...
        assert b.add(float(t), 100) is False
    assert b.add(9.99, 1) is True  # ... one byte more inside the same window is not


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


def test_oversized_text_frame_is_measured_in_bytes(client, plugin):
    with client.websocket_connect(WS) as conn:
        bridge = FakeBridge.spawned[-1]
        conn.send_text("\u20ac" * 22000)  # 66000 bytes of UTF-8
        with pytest.raises(WebSocketDisconnect) as exc:
            conn.receive_bytes()
        assert exc.value.code == plugin.CLOSE_TOO_BIG
    _wait_closed(bridge)
    assert bytes(bridge.written) == b""


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

def test_health_reports_contract_policy_and_limits(client, plugin, monkeypatch):
    body = client.get(PREFIX + "/health").json()
    assert body["ok"] is True and body["contract_ok"] is True and body["pty_available"] is True
    assert body["limit"] == 2 and body["active"] == 0 and body["idle_timeout_seconds"] == 1200
    assert body["allowed_users_configured"] is True and body["policy_readable"] is True
    monkeypatch.setattr(plugin, "_core", lambda: None)
    body = client.get(PREFIX + "/health").json()
    assert body["ok"] is False and body["contract_ok"] is False


def test_manifest_schema_and_versions_agree():
    import yaml
    from hermes_cli.plugins_manifest import validate_config_schema
    root = PLUGIN_FILE.parents[1]
    manifest = yaml.safe_load((root / "plugin.yaml").read_text(encoding="utf-8"))
    dashboard = json.loads((PLUGIN_FILE.parent / "manifest.json").read_text(encoding="utf-8"))
    mod = _load_plugin("hermes_dashboard_plugin_web_terminal_versions")
    assert manifest["version"] == dashboard["version"] == mod.PLUGIN_VERSION
    schema = manifest["config_schema"]
    assert schema["allowed_users"]["type"] == "list" and schema["allowed_users"]["required"] is True
    assert validate_config_schema("web-terminal", schema, {"allowed_users": ["test:owner"]}) == []
    assert len(validate_config_schema("web-terminal", schema, {})) == 1
