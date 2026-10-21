#!/usr/bin/env bash
# Tests deploy/robweb/nginx.conf in throwaway containers with a fake backend.
# Touches nothing live: own docker network, own port (default 18099), cleaned up on exit.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PORT="${PORT:-18099}"; ID="hdwtest$$"
NGINX_IMAGE="${NGINX_IMAGE:-nginx:1.27-alpine}"
cleanup() { docker rm -f "$ID-nginx" "$ID-stub" >/dev/null 2>&1 || true; docker network rm "$ID" >/dev/null 2>&1 || true; rm -rf "$WWW"; }
WWW="$(mktemp -d)"; trap cleanup EXIT
printf '<html><head><script src="./web-bridge.js?v=test"></script></head><body>app</body></html>' > "$WWW/index.html"
cp "$ROOT/web-bridge.js" "$ROOT/deploy/robweb/web-preview.js" "$WWW/"; chmod -R a+rX "$WWW"

docker network create "$ID" >/dev/null
docker run -d --name "$ID-stub" --network "$ID" --network-alias host.docker.internal \
  -v "$ROOT/tests/nginx/stub_backend.py:/stub.py:ro" python:3.12-alpine python3 /stub.py >/dev/null
docker run --rm --network "$ID" -v "$ROOT/deploy/robweb/nginx.conf:/etc/nginx/conf.d/default.conf:ro" "$NGINX_IMAGE" nginx -t
docker run -d --name "$ID-nginx" --network "$ID" -p "127.0.0.1:$PORT:8080" \
  -v "$ROOT/deploy/robweb/nginx.conf:/etc/nginx/conf.d/default.conf:ro" -v "$WWW:/usr/share/nginx/html:ro" "$NGINX_IMAGE" >/dev/null
for _ in $(seq 30); do curl -fs "http://127.0.0.1:$PORT/" >/dev/null 2>&1 && curl -fs "http://127.0.0.1:$PORT/api/ping" >/dev/null 2>&1 && break; sleep 0.5; done
python3 "$ROOT/tests/nginx/check_routing.py" "http://127.0.0.1:$PORT"
