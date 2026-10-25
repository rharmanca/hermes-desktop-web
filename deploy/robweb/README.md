# robweb deployment

Hardened, loopback-only Docker build of hermes-desktop-web used on robweb
(behind Cloudflare Access at desktop-web.robweb.dev). No secrets live here; auth is the
Hermes dashboard's own login plus Cloudflare Access.

## Pieces

| Piece | Where | Notes |
|---|---|---|
| Desktop Web container | `compose.yaml`, `Dockerfile`, `nginx.conf` | `127.0.0.1:18090`; read-only, 256 MiB, caps dropped; own network `hdw` (host interface `hdw0`, 10.231.90.0/24) |
| Browser pane relay | `preview-relay/relay.py` + `hermes-preview-relay.service` (systemd user unit) | Headless Chromium on `127.0.0.1`/`172.17.0.1:18091`; all page traffic goes through its checking proxy, which blocks private/internal addresses |
| Host firewall | `firewall.sh` | Relay `:18091` only from `lo` and `hdw0`; dashboard `:9119` only from `lo`, Docker bridges and `hdw0`. The relay has no login of its own (nginx `auth_request` guards `/preview-ws`), so this isolation is required |
| Web terminal | `../../gateway-plugin/web-terminal/` | Hermes dashboard plugin; enabled for the owner only (`allowed_users: ["basic:rob"]`) |
| Deploy / roll back | `ROLLBACK.md`, `tools/rollback.sh` | Tested rollback to the previous image and forward again |

## Deploy rule

GitHub is the source of truth: tests green, commit, push, GitHub Actions green, then
build and deploy (steps in `ROLLBACK.md`), then `tests/run-all.sh --live --browser`.
After changing the relay unit: `cp preview-relay/hermes-preview-relay.service ~/.config/systemd/user/`
and `systemctl --user daemon-reload && systemctl --user restart hermes-preview-relay`.
After changing firewall rules: `sudo deploy/robweb/firewall.sh && sudo netfilter-persistent save`.

## Branches

`main` tracks upstream; `fix/*` are upstream-proposed fixes; `robweb` = main + fixes + this
folder. Upgrade: `git fetch upstream && git merge upstream/main` on `robweb`, rebuild.
