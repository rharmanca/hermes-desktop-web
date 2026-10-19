# Reuse the already verified renderer; no dependency install or live state.
# Before building, verify this local base tag has image ID:
# sha256:bf0a456f362cfea05a74a1401d8e18543de6df769a970b1fad1ada1c6febdba0
FROM hermes-desktop-web-base:5e9d937-v0.22.0
COPY --chmod=644 deploy/robweb/nginx.conf /etc/nginx/conf.d/default.conf
COPY --chmod=644 web-bridge.js /usr/share/nginx/html/web-bridge.js
LABEL org.opencontainers.image.title="Hermes Desktop Web - local browser interface"
LABEL org.opencontainers.image.source="https://github.com/Ohrest88/hermes-desktop-web"
LABEL org.opencontainers.image.revision="5e9d93705160ae7d4610316ef4feaf05191e7244"
USER 101:101
