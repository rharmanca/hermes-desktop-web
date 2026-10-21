"""Round-3 QA regression (R5): capacity is freed only on positive cleanup confirmation.

Uses the real installed core ``PtyBridge.close``/``is_alive`` with an inert fake child, so
the core's early ``_closed`` flag and its suppressed final-terminate failure are exercised.
No real process is started or signalled; no live settings or credentials are read.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"


def _load(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    spec = importlib.util.spec_from_file_location("r5_round3_plugin", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class Socket:
    query_params: dict = {}
    client = types.SimpleNamespace(host="testclient")

    async def accept(self):
        pass

    async def receive(self):
        return {"type": "websocket.disconnect"}

    async def close(self, **kwargs):
        pass

    async def send_text(self, text):
        pass

    async def send_bytes(self, raw):
        pass


def _auth(ws):
    ws._hermes_auth_identity = {"provider": "test", "user_id": "owner"}
    return None, "test"


def _wire(mod, monkeypatch, bridge_cls):
    core = types.SimpleNamespace(_ws_auth_reason=_auth, _ws_request_is_allowed=lambda ws: True,
                                 PtyBridge=bridge_cls, PtyUnavailableError=RuntimeError)
    monkeypatch.setattr(mod, "_core", lambda: core)
    monkeypatch.setattr(mod, "_plugin_enabled", lambda: True)
    monkeypatch.setattr(mod, "_allowed_users", lambda: frozenset({"test:owner"}))
    monkeypatch.setattr(mod, "_shell_env", lambda: {"PATH": "/usr/bin:/bin"})
    monkeypatch.setattr(mod, "_audit", lambda *a, **k: None)
    reapers = []
    original = mod._reap

    def reap(slot, bridge):
        thread = original(slot, bridge)
        reapers.append(thread)
        return thread

    monkeypatch.setattr(mod, "_reap", reap)
    return reapers


def _dial(mod, reapers, n):
    for _ in range(n):
        asyncio.run(mod.terminal_ws(Socket()))
        for thread in reapers:
            thread.join(timeout=2)
            assert not thread.is_alive()


@pytest.mark.parametrize("termination_succeeds", [False, True])
def test_real_core_close_suppressed_failure_holds_capacity(monkeypatch, tmp_path,
                                                          termination_succeeds):
    from hermes_cli import pty_bridge
    from ptyprocess import PtyProcessError

    mod = _load(monkeypatch, tmp_path)
    ticks = iter(range(100000))
    monkeypatch.setattr(pty_bridge, "time", types.SimpleNamespace(monotonic=lambda: next(ticks)))
    children = []

    class Process:
        pid = 12345

        def __init__(self):
            self.alive = True

        def isalive(self):
            return self.alive

        def kill(self, sig):
            if termination_succeeds:
                self.alive = False

        def close(self, force=True):
            if self.alive:
                raise PtyProcessError("Could not terminate the child.")

    class Bridge:
        pid = 12345
        close = pty_bridge.PtyBridge.close
        is_alive = pty_bridge.PtyBridge.is_alive

        def __init__(self):
            self._closed = False
            self._pgid = None  # no group signalling or descendant enumeration
            self._proc = Process()
            children.append(self)

        @classmethod
        def spawn(cls, *a, **k):
            return cls()

        @staticmethod
        def is_available():
            return True

        def _discard_output(self, timeout):
            pass

        def read(self, timeout):
            return b""

        async def write(self, raw):
            return True

        def resize(self, cols, rows):
            pass

    reapers = _wire(mod, monkeypatch, Bridge)
    _dial(mod, reapers, mod.MAX_TERMINALS + 1)
    alive = sum(b._proc.isalive() for b in children)
    assert alive <= mod.MAX_TERMINALS
    assert mod._active_count() == alive
    assert mod._quarantined_count() == alive
    if termination_succeeds:
        assert alive == 0 and len(children) == mod.MAX_TERMINALS + 1
    else:
        assert alive == mod.MAX_TERMINALS and len(children) == mod.MAX_TERMINALS


class _Simple:
    spawned: list = []

    def __init__(self):
        self.closed = False
        self.pid = 4321
        type(self).spawned.append(self)

    @classmethod
    def spawn(cls, *a, **k):
        return cls()

    @staticmethod
    def is_available():
        return True

    def read(self, timeout):
        return None if self.closed else b""

    async def write(self, raw):
        return True

    def resize(self, cols, rows):
        pass

    def close(self):
        self.closed = True

    def is_alive(self):
        return not self.closed


def test_missing_private_contract_fails_closed(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)

    class NoProc(_Simple):
        spawned: list = []

    reapers = _wire(mod, monkeypatch, NoProc)
    _dial(mod, reapers, 1)
    assert mod._active_count() == 1 and mod._quarantined_count() == 1


def test_status_error_fails_closed(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)

    class Boom(_Simple):
        spawned: list = []
        _pgid = None

        @property
        def _proc(self):
            def isalive():
                raise OSError("synthetic status failure")
            return types.SimpleNamespace(pid=self.pid, isalive=isalive)

    reapers = _wire(mod, monkeypatch, Boom)
    _dial(mod, reapers, 1)
    assert mod._quarantined_count() == 1


@pytest.mark.parametrize("group_exc,freed", [(ProcessLookupError, True), (None, False),
                                             (PermissionError, False)])
def test_own_process_group_must_be_empty(monkeypatch, tmp_path, group_exc, freed):
    mod = _load(monkeypatch, tmp_path)
    calls = []

    def killpg(pgid, sig):
        calls.append((pgid, sig))
        if group_exc is not None:
            raise group_exc()

    monkeypatch.setattr(mod.os, "killpg", killpg)

    class Leader(_Simple):
        spawned: list = []

        @property
        def _pgid(self):
            return self.pid

        @property
        def _proc(self):
            return types.SimpleNamespace(pid=self.pid, isalive=lambda: not self.closed)

    reapers = _wire(mod, monkeypatch, Leader)
    _dial(mod, reapers, 1)
    assert calls == [(4321, 0)]  # probe only, never a real signal
    assert mod._active_count() == (0 if freed else 1)
    assert mod._quarantined_count() == (0 if freed else 1)
