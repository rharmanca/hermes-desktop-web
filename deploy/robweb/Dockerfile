# Reuse the already verified renderer; no dependency install or live state.
# Before building, verify this local base tag has image ID:
# sha256:bf0a456f362cfea05a74a1401d8e18543de6df769a970b1fad1ada1c6febdba0
FROM hermes-desktop-web-base:5e9d937-v0.22.0
COPY --chmod=644 deploy/robweb/nginx.conf /etc/nginx/conf.d/default.conf
COPY --chmod=644 web-bridge.js /usr/share/nginx/html/web-bridge.js
COPY --chmod=644 deploy/robweb/web-preview.js /usr/share/nginx/html/web-preview.js
# Server-approved Desktop plugins (read-only). The build fails if any file
# does not match its pinned checksum.
COPY --chmod=644 deploy/robweb/desktop-plugins/manifest.json /usr/share/nginx/html/desktop-plugins/manifest.json
COPY --chmod=644 deploy/robweb/desktop-plugins/profile-picker/plugin.js /usr/share/nginx/html/desktop-plugins/profile-picker/plugin.js
COPY --chmod=644 deploy/robweb/desktop-plugins/waypoint/plugin.js /usr/share/nginx/html/desktop-plugins/waypoint/plugin.js
COPY --chmod=644 deploy/robweb/desktop-plugins/SHA256SUMS /tmp/desktop-plugins.SHA256SUMS
# Cache-bust: reference the bridge by content hash so CDN/browser caches can never serve a stale copy.
USER root
RUN set -e; cd /usr/share/nginx/html/desktop-plugins && sha256sum -c /tmp/desktop-plugins.SHA256SUMS && rm /tmp/desktop-plugins.SHA256SUMS; \
    chmod 755 /usr/share/nginx/html/desktop-plugins /usr/share/nginx/html/desktop-plugins/profile-picker /usr/share/nginx/html/desktop-plugins/waypoint
RUN set -e; H=$(sha256sum /usr/share/nginx/html/web-bridge.js | cut -c1-12); \
    sed -i "s#src=\"./web-bridge.js\"#src=\"./web-bridge.js?v=$H\"#" /usr/share/nginx/html/index.html; \
    grep -q "web-bridge.js?v=$H" /usr/share/nginx/html/index.html; \
    P=$(sha256sum /usr/share/nginx/html/web-preview.js | cut -c1-12); \
    sed -i "s#<script type=\"module\"#<script src=\"./web-preview.js?v=$P\"></script><script type=\"module\"#" /usr/share/nginx/html/index.html; \
    grep -q "web-preview.js?v=$P" /usr/share/nginx/html/index.html
USER nginx
LABEL org.opencontainers.image.title="Hermes Desktop Web - local browser interface"
LABEL org.opencontainers.image.source="https://github.com/Ohrest88/hermes-desktop-web"
LABEL org.opencontainers.image.revision="5e9d93705160ae7d4610316ef4feaf05191e7244"
USER 101:101
