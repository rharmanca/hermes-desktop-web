#!/usr/bin/env python3
"""Routing tests for deploy/robweb/nginx.conf against a fake backend.
Run via tests/nginx/run_nginx_tests.sh (sets up the containers)."""
import base64, json, os, re, socket, sys, urllib.error, urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18099"
res = []
def check(name, cond, note=""): res.append((name, bool(cond), "" if cond else note))

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None
op = urllib.request.build_opener(NoRedirect)
def get(path, method="GET", headers=None, body=None):
    try:
        r = op.open(urllib.request.Request(BASE + path, data=body, method=method, headers=headers or {}), timeout=10)
        return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()

def upgrade(path, cookie=None):
    host, port = BASE.split("//")[1].split(":")
    s = socket.create_connection((host, int(port)), timeout=10)
    extra = f"Cookie: {cookie}\r\n" if cookie else ""
    s.sendall((f"GET {path} HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {base64.b64encode(os.urandom(16)).decode()}\r\nSec-WebSocket-Version: 13\r\n{extra}\r\n").encode())
    data = s.recv(4096).decode("latin1"); s.close()
    m = re.match(r"HTTP/1\.1 (\d+)", data)
    hdr = dict(l.split(": ", 1) for l in data.split("\r\n")[1:] if ": " in l)
    return (int(m.group(1)) if m else 0), {k.lower(): v for k, v in hdr.items()}

def echo(path, **kw):
    code, h, body = get(path, **kw)
    try: return code, h, json.loads(body)
    except Exception: return code, h, None

# Static app shell
code, h, body = get("/")
check("/ serves the app page", code == 200 and b"web-bridge.js" in body, f"{code}")
check("unknown app route falls back to the app page (SPA)", get("/some/deep/route")[2] == body)
for p in ["/", "/index.html", "/web-bridge.js", "/web-preview.js", "/some/route"]:
    c, hh, _ = get(p)
    for k, v in [("x-frame-options", "DENY"), ("x-content-type-options", "nosniff"), ("referrer-policy", "same-origin")]:
        check(f"{p}: header {k}", hh.get(k) == v, f"{hh.get(k)!r}")
for p in ["/web-bridge.js", "/web-preview.js"]:
    check(f"{p} never cached", "no-store" in get(p)[1].get("cache-control", ""))
check("/index.html revalidated", "no-cache" in get("/index.html")[1].get("cache-control", ""))
check("server version hidden", not re.search(r"\d", h.get("server", "")), h.get("server"))

# Performance: hashed assets cached long-term, static text compressed
c, hh, b = get("/assets/app-Ab12Cd.js")
check("hashed asset served", c == 200 and b.startswith(b"console.log"), f"{c}")
check("hashed asset cached 1 year + immutable", "max-age=31536000" in hh.get("cache-control", "") and "immutable" in hh.get("cache-control", ""), hh.get("cache-control"))
for k, v in [("x-frame-options", "DENY"), ("x-content-type-options", "nosniff"), ("referrer-policy", "same-origin")]:
    check(f"/assets/: header {k}", hh.get(k) == v, f"{hh.get(k)!r}")
c, hh, _ = get("/assets/app-Ab12Cd.js", headers={"Accept-Encoding": "gzip"})
check("asset gzip-compressed when browser asks", hh.get("content-encoding") == "gzip", hh.get("content-encoding"))
check("Vary: Accept-Encoding sent", "accept-encoding" in hh.get("vary", "").lower(), hh.get("vary"))
check("asset uncompressed when browser doesn't ask", get("/assets/app-Ab12Cd.js")[1].get("content-encoding") is None)
c, hh, b = get("/assets/missing-XYZ.js")
check("missing asset is 404, not the app page", c == 404 and b"web-bridge.js" not in b, f"{c}")
check("/index.html still revalidated (new releases show up)", "no-cache" in get("/index.html")[1].get("cache-control", ""))
check("web-bridge.js still never cached", "no-store" in get("/web-bridge.js")[1].get("cache-control", ""))
_, hh, _ = get("/api/sessions", headers={"Accept-Encoding": "gzip"})
check("API responses not compressed by nginx", hh.get("content-encoding") is None, hh.get("content-encoding"))

# API proxying
code, _, e = echo("/api/sessions?profile=hermes-admin")
check("/api/* goes to the Hermes backend", e and e["stub_port"] == 9119 and e["path"] == "/api/sessions?profile=hermes-admin", str(e)[:120])
_, _, e = echo("/api/x", headers={"Host": "desktop-web.example"})
check("Host header passed through", e and e["headers"].get("host") == "desktop-web.example", str(e and e["headers"].get("host")))
check("X-Forwarded-Host passed", e and e["headers"].get("x-forwarded-host") == "desktop-web.example")
check("no X-Forwarded-Prefix sent (app lives at /, not a sub-path)", e and not e["headers"].get("x-forwarded-prefix"))
_, _, e = echo("/api/fs/download?path=/x")
check("file download goes to backend", e and e["stub_port"] == 9119 and e["path"].startswith("/api/fs/download"))
_, _, e = echo("/login?next=/")
check("/login goes to backend", e and e["path"] == "/login?next=/")
_, _, e = echo("/auth/callback")
check("/auth goes to backend", e and e["path"] == "/auth/callback")
_, _, e = echo("/api/auth/ws-ticket", method="POST", body=b"{}", headers={"Content-Type": "application/json"})
check("POST ws-ticket reaches backend", e and e["method"] == "POST")

# Blocked plugins (never reach the backend)
for p in ["/api/pub", "/api/pub/x", "/api/events", "/api/events/", "/api/plugins/web-terminal", "/api/plugins/web-terminal/ws"]:
    c, _, b = get(p)
    check(f"{p} blocked at nginx (403, not forwarded)", c == 403 and b"stub_port" not in b, f"{c}")
for p in ["/api/publish", "/api/eventsx", "/api/plugins/other"]:
    c, _, e = echo(p)
    check(f"{p} NOT over-blocked", c == 200 and e and e["stub_port"] == 9119, f"{c}")

# WebSockets
c, hh = upgrade("/api/ws?ticket=abc")
check("chat websocket upgrades through nginx", c == 101 and hh.get("x-stub-port") == "9119", f"{c}")
c, hh = upgrade("/preview-ws")
check("preview websocket refused without sign-in (401/403, not an error)", c in (401, 403), f"{c}")
c, hh = upgrade("/preview-ws", cookie="session=bad")
check("preview websocket refused with bad cookie (401/403)", c in (401, 403), f"{c}")
c, hh = upgrade("/preview-ws", cookie="session=good")
check("preview websocket allowed when signed in, goes to relay /ws", c == 101 and hh.get("x-stub-port") == "18091" and hh.get("x-stub-path") == "/ws", f"{c} {hh}")
c, _, _ = get("/_preview_auth")
check("internal auth check not reachable from outside", c == 404, f"{c}")

# Server-approved Desktop plugins: only manifest + <name>/plugin.js, revalidated, never the SPA page.
c, hh, b = get("/desktop-plugins/manifest.json")
check("plugin manifest served", c == 200 and b"profile-picker" in b, f"{c}")
check("plugin manifest revalidated", "no-cache" in hh.get("cache-control", ""), hh.get("cache-control"))
c, hh, b = get("/desktop-plugins/profile-picker/plugin.js")
check("approved plugin file served", c == 200 and b"@hermes/plugin-sdk" in b, f"{c}")
c2, _, b2 = get("/desktop-plugins/waypoint/plugin.js")
check("approved waypoint plugin served", c2 == 200 and b"@hermes/plugin-sdk" in b2, f"{c2}")
c3, _, b3 = get("/desktop-plugins/gmail/plugin.js")
check("approved gmail plugin served", c3 == 200 and b"@hermes/plugin-sdk" in b3, f"{c3}")
for k, v in [("x-frame-options", "DENY"), ("x-content-type-options", "nosniff"), ("referrer-policy", "same-origin")]:
    check(f"plugin file: header {k}", hh.get(k) == v, f"{hh.get(k)!r}")
for p in ["/desktop-plugins/", "/desktop-plugins/profile-picker/", "/desktop-plugins/profile-picker/LICENSE",
          "/desktop-plugins/SHA256SUMS", "/desktop-plugins/missing/plugin.js", "/desktop-plugins/profile-picker/other.js",
          "/desktop-plugins/Evil/plugin.js", "/desktop-plugins/waypoint/LICENSE", "/desktop-plugins/waypoint/",
          "/desktop-plugins/gmail/NOTICE", "/desktop-plugins/gmail/"]:
    c, _, b = get(p)
    check(f"{p} not served (404, no listing, no app page)", c in (400, 404) and b"web-bridge.js" not in b and b"Index of" not in b, f"{c}")
for p in ["/desktop-plugins/../../../etc/passwd", "/desktop-plugins/%2e%2e/%2e%2e/%2e%2e/etc/passwd",
          "/desktop-plugins/..%2f..%2f..%2fetc/passwd", "/desktop-plugins/profile-picker/..%2f..%2f..%2f..%2fetc%2fpasswd"]:
    c, _, b = get(p)
    check(f"{p} cannot escape the web root", b"root:" not in b, f"{c}")

# Limits
c, _, _ = get("/api/upload", method="POST", body=b"x" * (17 * 1024 * 1024), headers={"Content-Type": "application/octet-stream"})
check("upload over 16 MB rejected (413)", c == 413, f"{c}")
c, _, e = echo("/api/upload", method="POST", body=b"x" * (15 * 1024 * 1024), headers={"Content-Type": "application/octet-stream"})
check("upload under 16 MB allowed", c == 200, f"{c}")

for n, ok, note in res: print(("PASS  " if ok else "FAIL  ") + n + (f"  — {note}" if note else ""))
f = sum(not r[1] for r in res)
print(f"---- {len(res) - f} passed, {f} failed")
sys.exit(1 if f else 0)
