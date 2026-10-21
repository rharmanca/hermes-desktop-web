#!/usr/bin/env python3
"""Fake Hermes backend + fake preview relay for nginx routing tests.

Answers every request with JSON describing what it received, so the test
can see exactly which requests nginx forwarded and with which headers.
/api/auth/me returns 200 only when the cookie `session=good` is present.
Listens on 9119 (backend) and 18091 (preview relay)."""
import base64, hashlib, json, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _echo(self):
        port = self.server.server_address[1]
        n = int(self.headers.get("Content-Length") or 0)
        if n: self.rfile.read(n)
        if self.path.startswith("/api/auth/me"):
            ok = "session=good" in (self.headers.get("Cookie") or "")
            self.send_response(200 if ok else 401); self.send_header("Content-Length", "0"); self.end_headers(); return
        if self.headers.get("Upgrade", "").lower() == "websocket":
            key = self.headers["Sec-WebSocket-Key"]
            acc = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            self.send_response(101); self.send_header("Upgrade", "websocket"); self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", acc); self.send_header("X-Stub-Port", str(port)); self.send_header("X-Stub-Path", self.path)
            self.end_headers(); return
        body = json.dumps({"stub_port": port, "path": self.path, "method": self.command,
                           "headers": {k.lower(): v for k, v in self.headers.items()}}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    do_GET = do_POST = do_PUT = do_DELETE = do_OPTIONS = _echo

for port in (18091, 9119):
    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    t = threading.Thread(target=srv.serve_forever, daemon=port != 9119); t.start()
