"""Independent acceptance edge cases for exact fd0a0c7. No real shells or live data.

All assertions express required safe behavior; no xfail/skip markers. The existing
70 tests are run separately. Fake-clock tests advance the plugin clock only, not
asyncio's scheduler. Every simulated shell is an inert Python object.
"""
from __future__ import annotations

import asyncio
import builtins
import contextlib
import importlib.util
import json
import sys
import time
import types
from pathlib import Path

import pytest


CANDIDATE = Path(__file__).resolve().parents[1]
PLUGIN = CANDIDATE / "dashboard/plugin_api.py"
OWNER = {"provider": "test", "user_id": "owner"}


def load_plugin():
    spec = importlib.util.spec_from_file_location("qa_contract_edges_plugin", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class Bridge:
    spawned = []

    def __init__(self, **kwargs):
        self.closed = False
        self.writes = []
        self.resizes = []
        self.size = (kwargs["cols"], kwargs["rows"])
        self.pid = 12345
        Bridge.spawned.append(self)

    @classmethod
    def spawn(cls, argv, **kwargs):
        return cls(**kwargs)

    @staticmethod
    def is_available():
        return True

    def read(self, timeout):
        time.sleep(0.002)
        return None if self.closed else b""

    async def write(self, data):
        self.writes.append(data)
        return True

    def resize(self, cols, rows):
        self.size = (cols, rows)
        self.resizes.append(self.size)

    def close(self):
        self.closed = True

    def is_alive(self):
        return not self.closed


class Socket:
    def __init__(self, messages=(), clock=None):
        self.messages = iter(messages)
        self.clock = clock
        self.query_params = {}
        self.client = types.SimpleNamespace(host="testclient")
        self.accepted = False
        self.closes = []

    async def accept(self):
        self.accepted = True

    async def receive(self):
        try:
            message = next(self.messages)
        except StopIteration:
            return {"type": "websocket.disconnect"}
        if self.clock is not None:
            self.clock.now, message = message
        return {"type": "websocket.receive", "bytes": message}

    async def send_bytes(self, data):
        pass

    async def send_text(self, data):
        pass

    async def close(self, code=1000, reason=""):
        self.closes.append(code)


def core_for(bridge=Bridge, result=(None, "test")):
    def auth(ws):
        ws._hermes_auth_identity = dict(OWNER)
        return result
    return types.SimpleNamespace(_ws_auth_reason=auth, _ws_request_is_allowed=lambda ws: True,
                                 PtyBridge=bridge, PtyUnavailableError=RuntimeError)


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    Bridge.spawned = []
    mod = load_plugin()
    mod.events = []
    mod.reapers = []
    monkeypatch.setattr(mod, "_core", lambda: core_for())
    monkeypatch.setattr(mod, "_plugin_enabled", lambda: True)
    monkeypatch.setattr(mod, "_allowed_users", lambda: frozenset({"test:owner"}))
    monkeypatch.setattr(mod, "_shell_env", lambda: {"PATH": "/usr/bin:/bin", "TERM": "xterm"})
    monkeypatch.setattr(mod, "_audit", lambda event, **fields: mod.events.append((event, fields)))
    original_reap = mod._reap

    def reap(slot, bridge):
        thread = original_reap(slot, bridge)
        mod.reapers.append(thread)
        return thread

    monkeypatch.setattr(mod, "_reap", reap)
    yield mod
    for thread in mod.reapers:
        thread.join(timeout=1)
        assert not thread.is_alive()


def fake_clock(monkeypatch, mod, clock):
    proxy = types.SimpleNamespace(**{name: getattr(asyncio, name) for name in dir(asyncio)})
    proxy.get_running_loop = lambda: types.SimpleNamespace(time=lambda: clock.now)
    monkeypatch.setattr(mod, "asyncio", proxy)
    time_proxy = types.SimpleNamespace(**{name: getattr(time, name) for name in dir(time)})
    time_proxy.time = time_proxy.monotonic = lambda: clock.now
    monkeypatch.setattr(mod, "time", time_proxy)


@pytest.mark.parametrize("start,end", [(0.99, 10.01), (9.99, 19.01), (100.99, 110.01)])
def test_r3_full_ten_second_window_in_handler(plugin, monkeypatch, start, end):
    clock = types.SimpleNamespace(now=0.0)
    fake_clock(monkeypatch, plugin, clock)
    frame = b"x" * plugin.MAX_INPUT_FRAME_BYTES
    count = plugin.MAX_INPUT_BYTES_PER_WINDOW // len(frame)
    sock = Socket([(start, frame)] * count + [(end, frame)] * count, clock)
    asyncio.run(plugin.terminal_ws(sock))
    written = sum(map(len, Bridge.spawned[-1].writes))
    print("R3_EDGE", json.dumps({"start": start, "end": end, "elapsed": end-start,
                               "bytes_written": written, "closes": sock.closes}))
    assert written <= plugin.MAX_INPUT_BYTES_PER_WINDOW
    assert plugin.CLOSE_POLICY in sock.closes


def test_r3_exact_limit_expires_after_full_window(plugin):
    budget = plugin._InputBudget(plugin.INPUT_WINDOW_S, plugin.MAX_INPUT_BYTES_PER_WINDOW)
    assert not budget.add(0.99, plugin.MAX_INPUT_BYTES_PER_WINDOW)
    assert not budget.add(11.0, plugin.MAX_INPUT_BYTES_PER_WINDOW)


def test_r5_completed_close_does_not_allow_unlimited_same_instant_spawns(plugin, monkeypatch):
    clock = types.SimpleNamespace(now=100.0)
    fake_clock(monkeypatch, plugin, clock)
    sockets = []

    async def exercise():
        for _ in range(64):
            sock = Socket()
            sockets.append(sock)
            await plugin.terminal_ws(sock)
            for thread in plugin.reapers:
                thread.join(timeout=1)
            assert not plugin._ACTIVE

    asyncio.run(exercise())
    print("R5_CHURN", json.dumps({"attempts": len(sockets), "spawns": len(Bridge.spawned),
                                "clock": clock.now, "denials": sum(not s.accepted for s in sockets)}))
    # The specification requires a time-based admission bound in addition to concurrency.
    # 64 is a diagnostic bound, not a proposed production quota.
    assert len(Bridge.spawned) < len(sockets), "no admission throttle engaged in 64 same-instant dials"


def test_r5_failed_reap_does_not_release_capacity(plugin, monkeypatch):
    class FailedClose(Bridge):
        def close(self):
            raise OSError("synthetic close failure before process termination")

    monkeypatch.setattr(plugin, "_core", lambda: core_for(FailedClose))
    for _ in range(plugin.MAX_TERMINALS + 1):
        asyncio.run(plugin.terminal_ws(Socket()))
        for thread in plugin.reapers:
            thread.join(timeout=1)
    live = sum(not b.closed for b in Bridge.spawned)
    print("R5_FAILED_REAP", json.dumps({"live_fake_shells": live, "slots": len(plugin._ACTIVE)}))
    assert live <= plugin.MAX_TERMINALS


@pytest.mark.parametrize("credential", [None, False, 1, {"kind": "internal"}, ["token"]])
def test_r6_malformed_auth_credential_type_refused(plugin, monkeypatch, credential):
    # Real contract is tuple[Optional[str], str], not tuple[Any, Any].
    core = core_for(result=(None, credential))
    monkeypatch.setitem(sys.modules, plugin._CORE_MODULE, core)
    resolver = load_plugin()._core
    monkeypatch.setattr(plugin, "_core", resolver)
    sock = Socket()
    asyncio.run(plugin.terminal_ws(sock))
    print("R6_AUTH_TYPE", json.dumps({"type": type(credential).__name__,
                                   "accepted": sock.accepted, "spawns": len(Bridge.spawned)}))
    assert not sock.accepted and not Bridge.spawned


def test_r6_valid_current_auth_shape_still_works(plugin):
    sock = Socket()
    asyncio.run(plugin.terminal_ws(sock))
    assert sock.accepted and len(Bridge.spawned) == 1


class PendingSocket(Socket):
    def __init__(self):
        super().__init__()
        self.receive_started = asyncio.Event()
        self.receive_task = None

    async def receive(self):
        self.receive_task = asyncio.current_task()
        self.receive_started.set()
        await asyncio.Event().wait()


def test_teardown_cancels_owned_receive_task(plugin):
    async def exercise():
        sock = PendingSocket()
        task = asyncio.create_task(plugin.terminal_ws(sock))
        await asyncio.wait_for(sock.receive_started.wait(), 1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        pending = not sock.receive_task.done()
        print("TEARDOWN_CANCEL", json.dumps({"handler_done": task.done(), "receive_still_pending": pending}))
        # Cleanup of the diagnostic itself, independent of the safety assertion.
        sock.receive_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sock.receive_task
        assert not pending, "handler returned while its receive task still owned the socket"

    asyncio.run(exercise())


def test_output_read_failure_ends_session_without_waiting_for_idle(plugin, monkeypatch):
    class BrokenRead(Bridge):
        def read(self, timeout):
            raise OSError("synthetic PTY read I/O error")

    monkeypatch.setattr(plugin, "_core", lambda: core_for(BrokenRead))
    monkeypatch.setattr(plugin, "IDLE_TIMEOUT_S", 60)
    monkeypatch.setattr(plugin, "_CLOSE_TIMEOUT_S", 0.02)

    async def exercise():
        sock = PendingSocket()
        task = asyncio.create_task(plugin.terminal_ws(sock))
        try:
            await asyncio.wait_for(sock.receive_started.wait(), 1)
            await asyncio.sleep(0.1)
            finished = task.done()
            closed = Bridge.spawned[-1].closed
            print("OUTPUT_READ_FAILURE", json.dumps({"handler_done": finished, "bridge_closed": closed,
                                                     "idle_limit": plugin.IDLE_TIMEOUT_S, "closes": sock.closes}))
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            if sock.receive_task:
                sock.receive_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sock.receive_task
        assert finished and closed, "output failure left shell open until idle timeout"

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["backup", "read_error"])
def test_r1_real_config_backup_and_read_error_refuse(monkeypatch, tmp_path, failure):
    from hermes_cli import config
    from hermes_cli.config_read_errors import FailedConfigRead
    home = tmp_path / "synthetic"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    path = home / "config.yaml"
    path.write_text("plugins:\n  enabled: [web-terminal]\n  entries:\n    web-terminal:\n      settings:\n        allowed_users: ['test:owner']\n")
    mod = load_plugin()
    assert mod._plugin_enabled() and mod._user_allowed(OWNER)
    config._LOAD_CONFIG_CACHE.pop(str(path), None)
    config._LAST_EXPANDED_CONFIG_BY_PATH.pop(str(path), None)
    if failure == "backup":
        path.write_text("plugins: [\n")
    else:
        original = builtins.open

        def failed_open(file, *args, **kwargs):
            if not isinstance(file, int) and Path(file) == path:
                raise PermissionError("synthetic current-config read denied")
            return original(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", failed_open)
    cfg = config.load_config_readonly()
    assert isinstance(cfg, FailedConfigRead)
    assert cfg["plugins"]["entries"]["web-terminal"]["settings"]["allowed_users"] == ["test:owner"]
    assert mod._policy_config() is None
    assert not mod._plugin_enabled() and not mod._user_allowed(OWNER)
    print("R1_EXTRA", json.dumps({"mode": failure, "fallback_type": type(cfg).__name__, "owner_allowed": False}))


# --- Implementer additions: threshold / recovery for the new bounds -------------------

def test_r5_admission_rate_threshold_and_recovery(plugin, monkeypatch):
    clock = types.SimpleNamespace(now=1000.0)
    fake_clock(monkeypatch, plugin, clock)
    plugin._ADMISSIONS = plugin._AdmissionRate(plugin.ADMISSION_WINDOW_S, plugin.MAX_ADMISSIONS_PER_WINDOW)

    def dial():
        sock = Socket()
        asyncio.run(plugin.terminal_ws(sock))
        for thread in plugin.reapers:
            thread.join(timeout=1)
        return sock

    for _ in range(plugin.MAX_ADMISSIONS_PER_WINDOW):
        dial()
    assert len(Bridge.spawned) == plugin.MAX_ADMISSIONS_PER_WINDOW
    refused = dial()
    assert len(Bridge.spawned) == plugin.MAX_ADMISSIONS_PER_WINDOW
    assert plugin.CLOSE_BUSY in refused.closes and not plugin._ACTIVE  # refusal holds no slot
    clock.now += plugin.ADMISSION_WINDOW_S - 0.01
    dial()
    assert len(Bridge.spawned) == plugin.MAX_ADMISSIONS_PER_WINDOW  # still inside the window
    clock.now += 0.02
    dial()
    assert len(Bridge.spawned) == plugin.MAX_ADMISSIONS_PER_WINDOW + 1  # recovered


def test_r5_quarantined_slot_reported_and_successful_close_frees(plugin, monkeypatch):
    class FailedClose(Bridge):
        def close(self):
            raise OSError("synthetic")

    monkeypatch.setattr(plugin, "_core", lambda: core_for(FailedClose))
    asyncio.run(plugin.terminal_ws(Socket()))
    for thread in plugin.reapers:
        thread.join(timeout=1)
    assert plugin._quarantined_count() == 1 and plugin._active_count() == 1
    monkeypatch.setattr(plugin, "_core", lambda: core_for())
    asyncio.run(plugin.terminal_ws(Socket()))
    for thread in plugin.reapers:
        thread.join(timeout=1)
    assert plugin._active_count() == 1  # the good close released its own slot only


@pytest.mark.parametrize("reason", [1, b"x", ["no"]])
def test_r6_malformed_auth_reason_type_refused(plugin, monkeypatch, reason):
    monkeypatch.setattr(plugin, "_core", lambda: core_for(result=(reason, "test")))
    sock = Socket()
    asyncio.run(plugin.terminal_ws(sock))
    assert not sock.accepted and not Bridge.spawned
