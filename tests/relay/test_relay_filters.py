#!/usr/bin/env python3
"""Offline tests for the browser-pane relay's address filter (deploy/robweb/preview-relay/relay.py).
No browser or network needed: DNS is faked. Run: python3 tests/relay/test_relay_filters.py"""
import asyncio, importlib.util, socket, sys, types, unittest
from pathlib import Path

RELAY = Path(__file__).resolve().parents[2] / "deploy/robweb/preview-relay/relay.py"
if "websockets" not in sys.modules:  # relay imports websockets; filters don't need it
    ws = types.ModuleType("websockets"); ws.ConnectionClosed = Exception
    sys.modules["websockets"] = ws
    sys.modules["websockets.asyncio"] = types.ModuleType("websockets.asyncio")
    srv = types.ModuleType("websockets.asyncio.server"); srv.serve = None
    sys.modules["websockets.asyncio.server"] = srv
spec = importlib.util.spec_from_file_location("relay", RELAY); relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)

FAKE_DNS = {"public.example": ["93.184.215.14"], "127.0.0.1.nip.io": ["127.0.0.1"],
            "lan.example": ["192.168.1.20"], "mixed.example": ["93.184.215.14", "10.0.0.5"],
            "v6loop.example": ["::1"], "meta.example": ["169.254.169.254"]}

class FakeLoopDNS:  # noqa
    def __init__(self): self.orig = None
    async def getaddrinfo(self, host, port, *a, **k):
        if host not in FAKE_DNS: raise socket.gaierror("no such host")
        return [(socket.AF_INET, 1, 6, "", (ip, 0)) for ip in FAKE_DNS[host]]

def blocked(url):
    async def run():
        loop = asyncio.get_running_loop(); fake = FakeLoopDNS()
        loop.getaddrinfo = fake.getaddrinfo  # instance patch on this test loop only
        return await relay.url_blocked(url)
    return asyncio.run(run())

class LiteralAddresses(unittest.TestCase):
    def test_private_literals_blocked(self):
        for u in ["http://127.0.0.1/", "http://127.0.0.1:9119/api/status", "http://[::1]/", "http://10.1.2.3/",
                  "http://172.17.0.1:18091/healthz", "http://192.168.0.1/", "http://169.254.169.254/latest/meta-data/",
                  "http://0.0.0.0/", "http://[::ffff:127.0.0.1]/", "http://100.64.0.1/"]:
            with self.subTest(u): self.assertTrue(relay.url_is_private(u))
    def test_internal_names_blocked(self):
        for u in ["http://localhost/", "http://LOCALHOST:9119/", "http://host.docker.internal/", "http://metadata.google.internal/",
                  "http://foo.localhost/", "http://printer.local/", "http://box.tail1234.ts.net/", "http:///nohost"]:
            with self.subTest(u): self.assertTrue(relay.url_is_private(u))
    def test_bad_schemes_blocked(self):
        for u in ["file:///etc/passwd", "chrome://settings", "javascript:alert(1)", "ftp://example.com/", "ws://example.com/"]:
            with self.subTest(u): self.assertTrue(relay.url_is_private(u))
    def test_harmless_schemes_allowed(self):
        for u in ["about:blank", "data:text/html,hi", "blob:https://example.com/x"]:
            with self.subTest(u): self.assertFalse(relay.url_is_private(u))
    def test_public_allowed(self):
        for u in ["https://en.wikipedia.org/wiki/Hermes", "http://8.8.8.8/", "https://example.com:8443/x?y=1"]:
            with self.subTest(u): self.assertFalse(relay.url_is_private(u))

class DnsAware(unittest.TestCase):
    """Regression: 127.0.0.1.nip.io used to get the preview straight into the relay's own /healthz."""
    def test_names_resolving_private_blocked(self):
        for h in ["127.0.0.1.nip.io", "lan.example", "mixed.example", "v6loop.example", "meta.example"]:
            with self.subTest(h): self.assertTrue(blocked(f"http://{h}:18091/healthz"))
    def test_public_name_allowed(self):
        self.assertFalse(blocked("https://public.example/"))
    def test_unresolvable_fails_closed(self):
        self.assertTrue(blocked("https://does-not-exist.example/"))
    def test_trailing_dot_and_case(self):
        self.assertTrue(blocked("http://LOCALHOST./")); self.assertTrue(blocked("http://127.0.0.1.NIP.IO./"))
    def test_literal_short_circuit(self):
        self.assertTrue(blocked("http://127.0.0.1/")); self.assertFalse(blocked("http://8.8.8.8/"))

class EgressProxy(unittest.TestCase):
    """The proxy is the real wall: resolve once, check, connect to the checked address."""
    def run_proxy(self, resolver, requests):
        async def go():
            # target: a local echo HTTP server (stands in for a public site)
            async def target(r, w):
                head = await r.readuntil(b"\r\n\r\n")
                w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"); await w.drain(); w.close()
                seen.append(head)
            seen = []
            tsrv = await asyncio.start_server(target, "127.0.0.1", 0); tport = tsrv.sockets[0].getsockname()[1]
            psrv = await asyncio.start_server(lambda r, w: relay.egress_handler(r, w, resolver), "127.0.0.1", 0)
            pport = psrv.sockets[0].getsockname()[1]
            out = []
            for req in requests:
                r, w = await asyncio.open_connection("127.0.0.1", pport)
                w.write(req.replace(b"TPORT", str(tport).encode())); await w.drain()
                out.append(await asyncio.wait_for(r.read(), 5)); w.close()
            tsrv.close(); psrv.close()
            return out, seen
        return asyncio.run(go())

    def test_private_target_refused_and_never_contacted(self):
        async def deny(h): return await relay.resolve_public(h)
        out, seen = self.run_proxy(deny, [b"CONNECT 127.0.0.1:TPORT HTTP/1.1\r\nHost: x\r\n\r\n",
                                          b"GET http://127.0.0.1:TPORT/ HTTP/1.1\r\nHost: x\r\n\r\n"])
        for o in out: self.assertTrue(o.startswith(b"HTTP/1.1 403"), o[:40])
        self.assertEqual(seen, [])

    def test_connects_to_the_checked_address_not_a_second_lookup(self):
        calls = []
        async def once(h): calls.append(h); return ["127.0.0.1"]  # pretend "public", pinned
        out, seen = self.run_proxy(once, [b"GET http://rebind.example:TPORT/x HTTP/1.1\r\nHost: rebind.example\r\nProxy-Connection: keep-alive\r\n\r\n"])
        self.assertTrue(out[0].startswith(b"HTTP/1.1 200"), out[0][:40]); self.assertEqual(calls, ["rebind.example"])
        self.assertIn(b"Connection: close", seen[0]); self.assertNotIn(b"keep-alive", seen[0].lower())

    def test_connect_tunnel_allowed_for_public(self):
        async def ok(h): return ["127.0.0.1"]
        out, _ = self.run_proxy(ok, [b"CONNECT site.example:TPORT HTTP/1.1\r\nHost: x\r\n\r\nGET / HTTP/1.1\r\nHost: x\r\n\r\n"])
        self.assertTrue(out[0].startswith(b"HTTP/1.1 200 Connection Established"))
        self.assertIn(b"ok", out[0])

    def test_garbage_and_non_http_refused(self):
        async def ok(h): return ["127.0.0.1"]
        out, seen = self.run_proxy(ok, [b"NONSENSE\r\n\r\n", b"GET ftp://x/ HTTP/1.1\r\n\r\n"])
        for o in out: self.assertTrue(o.startswith(b"HTTP/1.1 400"), o[:40])
        self.assertEqual(seen, [])

    def test_chromium_forced_through_proxy(self):
        src = RELAY.read_text()
        for flag in ["--proxy-server=http://127.0.0.1:", "--proxy-bypass-list=<-loopback>",
                     "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]:
            with self.subTest(flag): self.assertIn(flag, src)

class Limits(unittest.TestCase):
    def test_profile_name_rule(self):
        for ok in ["default", "hermes-admin", "work_lite", "a" * 64]:
            with self.subTest(ok): self.assertTrue(relay.PROFILE_RE.match(ok))
        for bad in ["", "../x", "A", "-x", "a/b", "a" * 65, "x y", "x\n"]:
            with self.subTest(bad): self.assertFalse(relay.PROFILE_RE.fullmatch(bad))
    def test_devtools_loopback_only(self):
        self.assertIn('"--remote-debugging-address=127.0.0.1"', RELAY.read_text())

if __name__ == "__main__":
    unittest.main(verbosity=1)
