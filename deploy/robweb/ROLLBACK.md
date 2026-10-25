# Deploy and roll back Desktop Web (robweb)

Live compose dir: `~/.hermes/profiles/hermes-admin/deployments/hermes-desktop-web`
Every deploy keeps the previous image as `hermes-desktop-web:rollback-pre-<new short sha>`.

## Deploy (after the commit is pushed and GitHub checks are green)
1. `docker tag hermes-desktop-web:0.22.0-5e9d937 hermes-desktop-web:rollback-pre-<sha>`
2. `docker build -f deploy/robweb/Dockerfile --label org.opencontainers.image.revision=<full sha> -t hermes-desktop-web:0.22.0-5e9d937 .`
3. Update `hermes.robweb.fork.commit` in the live compose.yaml; copy any compose change from `deploy/robweb/compose.yaml`.
4. `docker compose up -d --no-deps --force-recreate desktop-web`, then `tests/run-all.sh --live`.

## Roll back (about 1 minute)
    deploy/robweb/tools/rollback.sh <rollback tag>      # e.g. rollback-pre-0c950ff

It retags the rollback image as the running tag, recreates the container, waits for
`healthy`, and prints the running revision. Compose/network changes are rolled back by
restoring the live compose.yaml from its backup first. Firewall: `deploy/robweb/firewall.sh`
only adds rules; the pre-change rules are saved in `/etc/iptables/backup-*`.

Tested 2026-10-25: rolled back to rollback-pre-0c950ff (healthy, revision 9bee7a8) and forward again (healthy, 0c950ff); live smoke 15/15 afterwards.
