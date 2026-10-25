#!/usr/bin/env bash
# Usage: rollback.sh <rollback tag>   e.g. rollback-pre-0c950ff
set -euo pipefail
TAG="${1:?rollback tag, e.g. rollback-pre-0c950ff}"
DIR="${HDW_COMPOSE_DIR:-$HOME/.hermes/profiles/hermes-admin/deployments/hermes-desktop-web}"
RUN="hermes-desktop-web:0.22.0-5e9d937"
docker image inspect "hermes-desktop-web:$TAG" >/dev/null
docker tag "$RUN" "hermes-desktop-web:before-rollback-$(date +%Y%m%d%H%M%S)"
docker tag "hermes-desktop-web:$TAG" "$RUN"
(cd "$DIR" && docker compose up -d --no-deps --force-recreate desktop-web)
for _ in $(seq 60); do
  s=$(docker inspect hermes-desktop-web --format '{{.State.Health.Status}}'); [ "$s" = healthy ] && break; sleep 2
done
docker inspect hermes-desktop-web --format 'health={{.State.Health.Status}} revision={{index .Config.Labels "org.opencontainers.image.revision"}}'
[ "$s" = healthy ]
