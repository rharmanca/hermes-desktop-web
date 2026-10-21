# web-terminal — Hermes dashboard plugin (robweb-hardened)

Backend half of the Desktop Web **Terminal** tab: one WebSocket,
`/api/plugins/web-terminal/terminal`, that spawns the gateway user's login shell
behind a PTY (Hermes' own `PtyBridge`) and streams bytes to xterm.js in the
browser. Nothing runs on the agent side; `plugin.yaml` + `__init__.py` exist only
so `hermes plugins install/enable` can resolve the name.

**This is a host shell, not a sandbox.** Whoever it admits can do what the
dashboard's Unix user can do. Every control below is about *who* gets in and
*how much* they can push through the socket — not about what a shell can do.

## What the hardening changes (vs. upstream `0.1.0`)

| Concern | Behaviour |
|---|---|
| **Fail closed on the private API** | The plugin depends on two private Hermes APIs (`web_server_chat._ws_auth_reason` / `_ws_request_is_allowed` and `PtyBridge`). They are re-resolved on *every* upgrade and checked for the exact shape this plugin was written against: synchronous callables, a 2-tuple from the auth gate, exactly `True` from the origin gate. Anything else (missing, async, tuple/int/None result, exception) refuses before `accept()` (close `4401`/`4403`). Upstream returned `True` (open) when the import failed. This detects *shape* drift; a core change that keeps the shape but changes the meaning is not detectable here. |
| **Policy source** | `plugins.enabled`/`disabled` and `allowed_users` are read from the **launch home's** config, and only when it is **readable**. After a parse/read failure Hermes serves a *last-known-good* or default `FailedConfigRead`; the plugin treats that as unreadable → disabled, nobody (close `4404`). A request scoped to another profile's home (see next row) also counts as unreadable. |
| **No `?profile=`** | Plugin routes are mounted behind a profile-scope dependency, so a `?profile=X` request already sits in X's config/secret scope when the handler runs. The handler refuses it as its **first** step — before any policy read or spawn — on the WebSocket (`4403`) and on `GET /shell` and `GET /health` (HTTP `403`). Even if that check were bypassed, the policy reader refuses under a foreign home. |
| **Owner only** | A dashboard login proves *a* user; a host shell needs *the* operator. The identity the core gate stamped on the socket (`provider:user_id`, never a client parameter) must appear in `plugins.entries.web-terminal.settings.allowed_users`. Unset → **nobody** (close `4403`). The process-internal WS credential (`?internal=`) never opens a shell, whatever the list says. `GET /shell` (shell name + cwd preflight) is gated the same way (HTTP `403`). |
| **Concurrency** | 2 shells, counting shells whose PTY is still being torn down (the slot is released by the reaper when `PtyBridge.close()` has finished). 3rd dial gets a banner + close `1013`. A `close()` that raises does **not** free the slot (the shell may still be alive): it stays quarantined until the dashboard restarts; `/health` reports `quarantined`. |
| **Admission rate** | At most 12 shell starts per rolling 60 s, process-wide (`1013` banner), so connect/close churn is bounded even when each shell is reaped at once. Recovers as old starts age out. |
| **Error teardown** | A PTY read error ends the session through the same bounded teardown (`1011`), reaped once. Handler cancellation cancels and awaits its own pending receive. |
| **Idle** | 20 min without **keyboard** input closes the shell (`4409`). Every `ESC[RESIZE:cols;rows]` in a frame is consumed (last valid size applied, none reaches the shell) and none resets the clock. The watchdog is its own task, so a browser that stops reading output cannot stall it. A single PTY→browser send blocked for 30 s also ends the shell (`1013`). Shell exit → `4410` like `/api/pty`. |
| **Dimensions** | Dial-time `cols`/`rows` and every resize are clamped to the core resize ceiling (2000×1000; non-numeric or `< 1` → `80×24` / ignored). `PtyBridge.spawn` forwards raw values to ptyprocess, where anything above 65535 raises *inside the forked child*. |
| **Input bounds** | One input frame ≤ 64 KiB (`1009`). ≤ 1 MiB per rolling 10 s window per shell (`1008`). Every byte is counted for at least the full 10 s (50 ms accounting quanta rounded up, so 10–10.05 s, never shorter). |
| **Environment** | Dashboard env minus Hermes-managed secrets (`build_subprocess_env(scrub_secrets=True)`, the same scrub the agent's background commands get). Defence in depth, not a boundary: the shell is the operator's own login shell. |
| **Audit log** | `$HERMES_HOME/logs/web-terminal-audit.log`, JSON lines: `open` / `close` / `deny` / `error` with peer, server-stamped identity, pid, size, duration, byte counts. Never command text or terminal output. 5 MB rotation. Audit failure is logged, not a denial. |
| **Teardown** | The PTY is reaped on a plain daemon thread (not an executor future, which a server shutdown cancels), so a shell cannot outlive its socket; the thread releases the slot when the close sequence is done. The close handshake is bounded (5 s) and the handler never waits for a stalled peer's disconnect: it returns, and the server closes the transport. |

### Residual exposure (known, not fixed here)

* uvicorn's `ws_max_size` is the dashboard-wide 384 MiB attachment ceiling and
  cannot be lowered per route. A WebSocket message is reassembled by the
  transport *before* this handler rejects it, so the 64 KiB check bounds what
  reaches the PTY, not transport memory.
* PTY output has no quota. A permitted command can print far more than was typed;
  backpressure (and the 30 s stall close) bounds buffering, not volume.

`GET /health` reports `contract_ok`, `pty_available`, `policy_readable`,
`allowed_users_configured`, `active`/`limit`, `idle_timeout_seconds`.

## Configuration (required — the terminal refuses everyone until this is set)

`allowed_users` lists `provider:user_id` pairs exactly as the dashboard's auth
layer reports them. For the built-in username/password provider that is
`basic:<username>`; for an OIDC provider it is `<provider-name>:<subject>`. The
pairs appear in `$HERMES_HOME/logs/dashboard-auth.log` as the `provider` and
`user_id` fields of `login_success` / `ws_ticket_minted` events.

```bash
hermes config set plugins.entries.web-terminal.settings.allowed_users '["basic:<username>"]'
```

which stores:

```yaml
plugins:
  entries:
    web-terminal:
      settings:
        allowed_users:
          - basic:<username>
```

The list is re-read on every dial — no restart needed to add or remove a user.
`plugin.yaml` declares the key as `required` in its `config_schema`, so an
unset value is also surfaced as a plugin-load warning (advisory; the runtime
default-deny is the actual gate).

## Install / enable (machine where Hermes runs)

```bash
# HERMES_HOME must be the hermes ROOT (the dashboard scans <root>/plugins/<name>/dashboard/manifest.json)
hermes plugins install rharmanca/hermes-desktop-web#gateway-plugin/web-terminal --ref <commit-sha> --no-enable
hermes plugins enable web-terminal
```

No dashboard restart is needed: the first authenticated HTTP request to
`/api/plugins/web-terminal/...` hot-mounts the router. Disable with
`hermes plugins disable web-terminal` (new dials refused immediately; the router
stays mounted until restart but the handler refuses).

## Tests

```bash
mkdir -p /tmp/wt-home && cd <repo> && \
  env -u HERMES_PROFILE HERMES_HOME=/tmp/wt-home PYTHONPATH=<hermes-agent> PYTHONDONTWRITEBYTECODE=1 \
  <hermes-agent>/.venv/bin/python -m pytest gateway-plugin/web-terminal/tests -q -p no:cacheprovider -o addopts=""
```

Use a throwaway `HERMES_HOME`; the tests never need a live home. `PYTHONPATH` must name the
Hermes source checkout: without it the venv cannot import core helpers (e.g. `hermes_yaml`),
so spawns are refused and many tests fail. That is the environment, not the plugin — the
plugin still refuses safely in that state.

`tests/test_qa_r1_r7_regressions.py` carries the independent QA probes for findings R1–R7
(adapted to 0.2.1 interfaces only); every one must pass.

**Correction note (0.2.1).** The WIP commit `bd0fde9` says "22 tests failing". That count came
from a run without `PYTHONPATH` set to the Hermes source; with the command above all supplied
tests pass on `bd0fde9`. The pushed history is left as is; this note supersedes that message.

Several tests run against the real `hermes_cli`: the private-API contract
(`test_real_core_still_satisfies_contract`), a real PTY spawned at the clamp
ceiling (`stty size`), the real config loader's last-known-good behaviour on a
broken file, and the plugin mounted through the dashboard's real mount helper
behind the real `?profile=` scope dependency. After a Hermes update, a failure in
any of them means a core seam moved and the terminal has (correctly) switched
itself off — chat is unaffected. The rest use a fake PTY bridge and a fake core
gate that stamps a test identity.

## After every Hermes update

1. Run the tests above.
2. `GET /api/plugins/web-terminal/health` → `contract_ok: true`, `policy_readable: true`, `allowed_users_configured: true`.
3. Open the Terminal tab once as the allowed user.
