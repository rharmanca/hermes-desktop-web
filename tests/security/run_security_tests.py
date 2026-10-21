#!/usr/bin/env python3
"""Security tests for a running Desktop Web deployment.

Tries to get in without signing in, with forged/reused passes, via path
tricks, oversized uploads and cross-site requests, and checks the container
is locked down. Read-only: nothing is changed on the server.

Usage:
  python3 tests/security/run_security_tests.py                  # anonymous attacks + container checks
  python3 tests/security/run_security_tests.py --signed-in      # + attacks from a signed-in browser (CDP)
Options: --base http://127.0.0.1:18090  --cdp http://127.0.0.1:9222  --no-docker
Exit 0 = all passed. WARN lines are findings that are not failures by policy.
Stdlib only for the anonymous set; --signed-in needs playwright.
"""
import argparse, base64, json, os, re, socket, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

results = []
def record(name, status, note=""): results.append((name, status, note))
def check(name, cond, note=""): record(name, "PASS" if cond else "FAIL", "" if cond else note)

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None
OPENER = urllib.request.build_opener(NoRedirect)

def http(base, path, method="GET", body=None, headers=None, timeout=15):
    req = urllib.request.Request(base + path, data=body, method=method, headers=headers or {})
    try:
        r = OPENER.open(req, timeout=timeout)
        return r.status, dict(r.headers), r.read(200000)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read(200000)
    except (ConnectionResetError, BrokenPipeError) as e:
        return 0, {}, str(e).encode()

def raw_ws_upgrade(base, path, extra=""):
    """Send a WebSocket upgrade by hand; return the HTTP status line code."""
    u = urllib.parse.urlparse(base)
    s = socket.create_connection((u.hostname, u.port or 80), timeout=10)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((f"GET {path} HTTP/1.1\r\nHost: {u.netloc}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n{extra}\r\n").encode())
    data = b""
    try:
        while b"\r\n" not in data:
            chunk = s.recv(4096)
            if not chunk: break
            data += chunk
    except socket.timeout:
        pass
    s.close()
    m = re.match(rb"HTTP/1\.[01] (\d{3})", data)
    return int(m.group(1)) if m else 0

def anonymous(a):
    b = a.base
    # 1. Everything private needs a sign-in.
    for method, path in [("GET", "/api/profiles"), ("GET", "/api/sessions"), ("GET", "/api/config"),
                         ("POST", "/api/auth/ws-ticket"), ("GET", "/api/fs/read-text?path=/etc/passwd"),
                         ("GET", "/api/fs/download?path=/etc/passwd"), ("GET", "/api/fs/read-data-url?path=/etc/hostname"),
                         ("GET", "/api/auth/me"), ("GET", "/api/sessions?profile=hermes-admin")]:
        code, _, body = http(b, path, method)
        check(f"no sign-in: {method} {path} is refused", code in (401, 403) and b"root:" not in body, f"got {code}")
    # 2. Optional plugins stay switched off (even signed in).
    for path in ["/api/pub", "/api/events", "/api/plugins/web-terminal/", "/api/plugins/web-terminal/pty", "/api/pub/x"]:
        code, _, _ = http(b, path)
        check(f"disabled route {path} returns 403", code == 403, f"got {code}")
    # 3. Live connections without a valid pass are refused.
    for label, path in [("no ticket", "/api/ws"), ("forged ticket", "/api/ws?ticket=forged-" + "A" * 40),
                        ("empty ticket", "/api/ws?ticket="), ("injection ticket", "/api/ws?ticket=%27%20OR%201%3D1--"),
                        ("terminal no ticket", "/api/pty"), ("preview relay no cookie", "/preview-ws"),
                        ("web-terminal ws", "/api/plugins/web-terminal/ws")]:
        code = raw_ws_upgrade(b, path)
        check(f"websocket {label} is refused (401/403)", code in (401, 403), f"got {code or 'no answer'}")
    code = raw_ws_upgrade(b, "/api/ws?ticket=forged", "Origin: https://evil.example\r\n")
    check("websocket from a foreign site with forged pass is refused (401/403)", code in (401, 403), f"got {code}")
    # 4. Path tricks can't read server files.
    for path in ["/../../../../etc/passwd", "/%2e%2e/%2e%2e/%2e%2e/etc/passwd", "/..%2f..%2f..%2fetc%2fpasswd",
                 "/static/..%252f..%252fetc/passwd", "/web-bridge.js/../../../etc/passwd", "//etc/passwd",
                 "/api/../../etc/passwd", "/index.html%00.js"]:
        code, _, body = http(b, path)
        check(f"path trick {path} leaks nothing", b"root:x:" not in body and b"nginx:" not in body[:5000], f"got {code}")
    # 5. Security headers + no version banner.
    code, h, body = http(b, "/")
    hl = {k.lower(): v for k, v in h.items()}
    check("header X-Content-Type-Options: nosniff", hl.get("x-content-type-options") == "nosniff", str(hl.get("x-content-type-options")))
    check("header X-Frame-Options: DENY (no clickjacking)", hl.get("x-frame-options", "").upper() == "DENY", str(hl.get("x-frame-options")))
    check("header Referrer-Policy set", bool(hl.get("referrer-policy")), "missing")
    check("server does not reveal its version", not re.search(r"\d", hl.get("server", "")), hl.get("server", ""))
    for p in ["/web-bridge.js", "/web-preview.js", "/index.html"]:
        _, h2, _ = http(b, p)
        cc = {k.lower(): v for k, v in h2.items()}.get("cache-control", "")
        check(f"{p} cannot be cached stale", "no-cache" in cc, cc or "missing")
    # 6. No cross-site access.
    code, h, _ = http(b, "/api/profiles", "OPTIONS", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    acao = {k.lower(): v for k, v in h.items()}.get("access-control-allow-origin", "")
    check("other websites are not allowed to call the API (CORS)", acao not in ("*", "https://evil.example"), acao)
    code, h, _ = http(b, "/api/auth/ws-ticket", "POST", headers={"Origin": "https://evil.example"})
    check("foreign-site ticket request without sign-in is refused", code in (401, 403), f"got {code}")
    # 7. Oversized uploads are cut off.
    big = b"x" * (17 * 1024 * 1024)
    code, _, _ = http(b, "/api/upload", "POST", big, {"Content-Type": "application/octet-stream"}, timeout=60)
    check("17 MB upload is rejected by the size limit (413)", code == 413, f"got {code or 'connection reset'}")
    # 8. Login page cannot be used to bounce users to another site.
    for nxt in ["https://evil.example", "//evil.example", "/\\evil.example", "javascript:alert(1)"]:
        code, h, body = http(b, "/login?next=" + urllib.parse.quote(nxt, safe=""))
        loc = {k.lower(): v for k, v in h.items()}.get("location", "")
        check(f"login next={nxt} does not redirect off-site", "evil.example" not in loc and not loc.startswith("javascript"), loc)
    # 9. Garbage requests don't break the server.
    code, _, _ = http(b, "/api/profiles", "POST", b"{not json", {"Content-Type": "application/json"})
    check("malformed JSON is refused cleanly", code in (400, 401, 403, 405, 422), f"got {code}")
    code, _, _ = http(b, "/" + "a" * 9000)
    check("very long URL is handled (no crash)", code in (200, 404, 414, 400), f"got {code}")
    code, _, _ = http(b, "/")
    check("server still healthy after attacks", code == 200, f"got {code}")

def container(a):
    if a.no_docker: return
    try:
        info = json.loads(subprocess.check_output(["docker", "inspect", "hermes-desktop-web"], text=True))[0]
    except Exception as e:
        record("container inspect", "FAIL", "could not inspect container: " + str(e)[:100]); return
    hc = info["HostConfig"]
    check("container filesystem is read-only", hc.get("ReadonlyRootfs") is True)
    check("container drops all Linux capabilities", "ALL" in (hc.get("CapDrop") or []), str(hc.get("CapDrop")))
    check("container cannot gain privileges", any("no-new-privileges" in s for s in hc.get("SecurityOpt") or []))
    check("container is not privileged", hc.get("Privileged") is False)
    check("container runs as non-root user", info["Config"].get("User") not in ("", "0", "root"), info["Config"].get("User"))
    binds = [b2 for v in (hc.get("PortBindings") or {}).values() for b2 in v or []]
    check("container port only on loopback (127.0.0.1)", binds and all(x.get("HostIp") == "127.0.0.1" for x in binds), str(binds))
    check("container has a process limit", (hc.get("PidsLimit") or 0) > 0)
    check("container has a memory limit", (hc.get("Memory") or 0) > 0)
    check("no host folders mounted into the container", not info.get("Mounts") or all(m["Type"] == "tmpfs" for m in info["Mounts"]), str([m.get("Source") for m in info.get("Mounts", [])]))
    check("auto-update (watchtower) is off", info["Config"]["Labels"].get("com.centurylinklabs.watchtower.enable") == "false")
    # What else is listening that the public could reach?
    try:
        ss = subprocess.check_output(["ss", "-ltnH"], text=True)
        for port, label in [("18091", "preview relay"), ("9222", "automation browser DevTools")]:
            lines = [l for l in ss.splitlines() if re.search(rf":{port}\s", l)]
            exposed = [l for l in lines if re.search(rf"(0\.0\.0\.0|\[::\]|\*):{port}\s", l)]
            if lines: check(f"{label} (:{port}) not listening on all interfaces", not exposed, exposed[0].split()[3] if exposed else "")
        if re.search(r"(0\.0\.0\.0|\*):9119\s", ss):
            record("Hermes dashboard (:9119) listens on all interfaces", "WARN", "reachable from the local network unless a firewall blocks it")
    except Exception as e:
        record("listening ports", "FAIL", "could not list ports: " + str(e)[:80])

def signed_in(a):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = p.chromium.connect_over_cdp(a.cdp).contexts[0]
        pg = ctx.new_page()
        try:
            pg.goto(a.base + "/"); pg.wait_for_selector("[role=textbox][aria-label=Message]", timeout=45000)
            ws_probe = """async url => new Promise(res => { const w = new WebSocket(url); let done=false;
                const fin = v => { if(!done){done=true; try{w.close()}catch{}; res(v)} };
                w.onopen = () => setTimeout(() => fin(w.readyState===1?'open':'closed'), 1500);
                w.onclose = e => fin('closed:'+e.code); setTimeout(() => fin('timeout'), 8000) })"""
            url = pg.evaluate("window.hermesDesktop.getGatewayWsUrl()")
            first = pg.evaluate(ws_probe, url)
            check("signed in: fresh pass opens a live connection", first == "open", first)
            second = pg.evaluate(ws_probe, url)
            check("signed in: a used pass cannot be reused", second != "open", second)
            tampered = url[:-3] + ("AAA" if not url.endswith("AAA") else "BBB")
            check("signed in: an altered pass is refused", pg.evaluate(ws_probe, tampered) != "open")
            if a.slow:
                stale = pg.evaluate("window.hermesDesktop.getGatewayWsUrl()")
                time.sleep(35)
                check("signed in: a pass expires after 30 seconds", pg.evaluate(ws_probe, stale) != "open")
            for prof in ["../../etc", "..%2F..%2Fetc", "hermes-admin/../../x", "<script>", "a" * 300]:
                r = pg.evaluate("p => window.hermesDesktop.api({path:'/api/sessions', profile:p}).then(()=>'ok', e=>String(e).slice(0,120))", prof)
                check(f"signed in: bad profile name {prof[:20]!r} is refused", r != "ok" and "root:" not in r and "Traceback" not in r, r)
            r = pg.evaluate("fetch('/api/fs/read-text?path=/etc/shadow',{credentials:'same-origin'}).then(r=>r.status+':'+r.headers.get('content-type'))")
            check("signed in: protected system file /etc/shadow cannot be read", not r.startswith("200"), r)
        finally:
            pg.close()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:18090")
    ap.add_argument("--cdp", default="http://127.0.0.1:9222")
    ap.add_argument("--signed-in", action="store_true")
    ap.add_argument("--slow", action="store_true", help="include the 35-second ticket-expiry test")
    ap.add_argument("--no-docker", action="store_true")
    a = ap.parse_args()
    anonymous(a); container(a)
    if a.signed_in: signed_in(a)
    for n, s, note in results: print(f"{s:4}  {n}" + (f"  — {note}" if note else ""))
    f = sum(r[1] == "FAIL" for r in results)
    print(f"---- {sum(r[1]=='PASS' for r in results)} passed, {f} failed, {sum(r[1]=='WARN' for r in results)} warnings")
    sys.exit(1 if f else 0)

if __name__ == "__main__":
    main()
