# web-terminal — Hermes dashboard plugin (robweb-hardened)

Backend half of the Desktop Web **Terminal** tab: one WebSocket,
`/api/plugins/web-terminal/terminal`, that spawns the gateway user's login shell
behind a PTY (Hermes' own `PtyBridge`) and streams bytes to xterm.js in the
browser. Nothing runs on the agent side; `plugin.yaml` + `__init__.py` exist only
so `hermes plugins install/enable` can resolve the name.

## What the hardening changes (vs. upstream `0.1.0`)

| Concern | Behaviour |
|---|---|
| **Fail closed** | The plugin depends on two private Hermes APIs (`web_server_chat._ws_auth_reason` / `_ws_request_is_allowed` and `PtyBridge`). They are re-resolved on *every* upgrade; if either is missing or changed shape the dial is refused (close `4401`) before `accept()`. Upstream returned `True` (open) when the import failed. |
| **Enabled check on the WS leg** | The dashboard's `plugins.enabled` gate is HTTP-only middleware; the handler repeats it, so disabling the plugin stops new shells without a restart (close `4404`). |
| **Credential → origin order** | Same gates and close codes as the dashboard's own `/api/pty` (`4401` auth, `4403` host/origin/peer). |
| **No `?profile=`** | Plugin routes are mounted behind a profile-scope dependency; a dial naming a profile would run under that profile's config/secret scope. Refused (`4403`) before any config is read. |
| **Owner only** | A dashboard login proves *a* user; a host shell needs *the* operator. The identity the core gate stamped on the socket (`provider:user_id`, never a client parameter) must appear in `plugins.entries.web-terminal.settings.allowed_users`. Unset or unreadable → **nobody** (close `4403`). `GET /shell` (shell name + cwd preflight) is gated the same way (HTTP `403`). |
| **Limits** | 2 concurrent shells (3rd gets a banner + close `1013`); 20 min without **keyboard** input closes the shell (`4409`) — resize frames are layout, not typing, and do not reset that clock; shell exit → `4410` like `/api/pty`. |
| **Dimensions** | Dial-time `cols`/`rows` and every `ESC[RESIZE:cols;rows]` are clamped to the core resize ceiling (2000×1000; non-numeric or `< 1` → `80×24` / ignored). `PtyBridge.spawn` forwards raw values to ptyprocess, where anything above 65535 raises *inside the forked child*. |
| **Input bounds** | One input frame ≤ 64 KiB (`1009`), ≤ 1 MiB per 10 s per shell (`1008`). uvicorn's `ws_max_size` is the dashboard-wide 384 MiB attachment ceiling and cannot be lowered per route. |
| **Environment** | Dashboard env minus Hermes-managed secrets (`build_subprocess_env(scrub_secrets=True)`, the same scrub the agent's background commands get). The dashboard service itself carries no `.env` values in its environment; the shell is the operator's own login shell, so this is defence in depth, not a boundary. |
| **Audit log** | `$HERMES_HOME/logs/web-terminal-audit.log`, JSON lines: `open` / `close` / `deny` / `error` with peer, server-stamped identity, pid, size, duration, byte counts. Never command text or terminal output. 5 MB rotation. |
| **Teardown** | The PTY is reaped on a plain thread (not an executor future, which a server shutdown cancels), so a shell cannot outlive its socket. |

`GET /health` reports `contract_ok`, `pty_available`, `active`/`limit`,
`idle_timeout_seconds` and `allowed_users_configured`.

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
unset value is also surfaced as a plugin-load warning.

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
cd <repo> && HERMES_HOME=~/.hermes PYTHONPATH=<hermes-agent> \
  <hermes-agent>/.venv/bin/python -m pytest gateway-plugin/web-terminal/tests -q -o addopts=""
```

Two tests run against the real `hermes_cli`: `test_real_core_still_satisfies_contract`
(after a Hermes update, a failure there means the private API moved and the
terminal has correctly switched itself off — chat is unaffected) and
`test_real_bridge_spawns_at_the_clamped_ceiling` (a real PTY at the clamp ceiling
reports the expected `stty size`). The rest use a fake PTY bridge and a fake
core gate that stamps a test identity.

## After every Hermes update

1. Run the tests above.
2. `GET /api/plugins/web-terminal/health` → `contract_ok: true`, `allowed_users_configured: true`.
3. Open the Terminal tab once as the allowed user.
