#!/usr/bin/env python3
"""Adversarial test for the web-terminal plugin against a THROWAWAY dashboard.

Never touches the live dashboard: it starts its own Hermes dashboard with a
temporary HERMES_HOME, a random one-time password and a random port, copies
gateway-plugin/web-terminal into it, attacks it, then tears everything down.

Needs a real Hermes checkout (HERMES_AGENT_SRC, default ~/.hermes/hermes-agent)
and a non-loopback local address (default the docker0 bridge 172.17.0.1, so
the dashboard's login gate engages). Exits 77 (skip) when either is missing.

    python3 tests/web-terminal/run_terminal_attack.py
"""
import asyncio, json, os, re, secrets, shutil, signal, socket, subprocess, sys, tempfile, time

try:
    import aiohttp, websockets
except ImportError:
    print("SKIP: needs aiohttp + websockets"); sys.exit(77)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.expanduser(os.environ.get("HERMES_AGENT_SRC", "~/.hermes/hermes-agent"))
PY = os.path.join(SRC, ".venv/bin/python")
HOST = os.environ.get("WT_TEST_HOST", "172.17.0.1")
CANARY = "OPENROUTER_API_KEY"  # a Hermes-managed secret name: the shell scrub must drop it
R = []


def rec(name, ok, detail=""):
    R.append((name, ok)); print(("PASS  " if ok else "FAIL  ") + name, detail, flush=True)


def free_port():
    with socket.socket() as s:
        s.bind((HOST, 0)); return s.getsockname()[1]


class Env:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="wt-attack-")
        self.home = os.path.join(self.tmp, "home"); os.makedirs(os.path.join(self.home, "plugins"))
        shutil.copytree(os.path.join(REPO, "gateway-plugin/web-terminal"),
                        os.path.join(self.home, "plugins/web-terminal"),
                        ignore=shutil.ignore_patterns("tests", "__pycache__"))
        self.cfg = os.path.join(self.home, "config.yaml")
        self.write_cfg('["basic:owner"]', True)
        self.pw = secrets.token_urlsafe(18); self.port = free_port()
        self.base = f"http://{HOST}:{self.port}"
        self.ws = f"ws://{HOST}:{self.port}/api/plugins/web-terminal/terminal"

    def write_cfg(self, allowed, enabled):
        with open(self.cfg, "w") as f:
            f.write("plugins:\n  enabled: [%s]\n  entries:\n    web-terminal:\n      settings:\n"
                    "        allowed_users: %s\n" % ("web-terminal" if enabled else "", allowed))

    def start(self):
        env = {"HOME": os.environ.get("HOME", "/tmp"), "PATH": "/usr/local/bin:/usr/bin:/bin",
               "SHELL": "/bin/bash", "LANG": "C.UTF-8", "HERMES_HOME": self.home,
               "HERMES_DASHBOARD_BASIC_AUTH_USERNAME": "owner",
               "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD": self.pw,
               "HERMES_DASHBOARD_BASIC_AUTH_SECRET": secrets.token_hex(32),
               CANARY: "canary-" + secrets.token_hex(4)}
        self.log = open(os.path.join(self.tmp, "dash.log"), "w")
        self.proc = subprocess.Popen([PY, "-m", "hermes_cli.main", "dashboard", "--host", HOST,
                                      "--port", str(self.port), "--no-open"], cwd=SRC, env=env,
                                     stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
        end = time.time() + 90
        while time.time() < end:
            if self.proc.poll() is not None: break
            try:
                with socket.create_connection((HOST, self.port), 1): return True
            except OSError: time.sleep(1)
        return False

    def stop(self):
        try: os.killpg(self.proc.pid, signal.SIGTERM); self.proc.wait(15)
        except Exception:
            try: os.killpg(self.proc.pid, signal.SIGKILL)
            except Exception: pass
        shutil.rmtree(self.tmp, ignore_errors=True)


E = None
DENY = (4401, 4403, "http401", "http403")


async def login(s, pw=None, user="owner"):
    async with s.post(E.base + "/auth/password-login",
                      json={"provider": "basic", "username": user, "password": pw or E.pw}) as r:
        return r.status


async def ticket(s):
    async with s.post(E.base + "/api/auth/ws-ticket", headers={"Origin": E.base}) as r:
        return (await r.json()).get("ticket") if r.status == 200 else None


async def dial(q="", origin=None):
    try:
        ws = await websockets.connect(E.ws + q, origin=origin or E.base, max_size=None, open_timeout=10)
    except websockets.InvalidStatus as e: return None, f"http{e.response.status_code}"
    except Exception as e: return None, type(e).__name__
    try:
        await asyncio.wait_for(ws.recv(), 3)
        await asyncio.wait_for(ws.recv(), 1)  # a refusal sends its reason, then closes
        return ws, "open"
    except websockets.ConnectionClosed as e: return None, e.rcvd.code if e.rcvd else "closed"
    except asyncio.TimeoutError: return ws, "open"


async def run(ws, cmd, want, t=8):
    # Marker split with quotes so the echoed command line can't match `want`.
    await ws.send(cmd.encode() + b"\r"); buf = b""; end = time.time() + t
    while time.time() < end and want.encode() not in buf:
        try:
            m = await asyncio.wait_for(ws.recv(), 1); buf += m if isinstance(m, bytes) else m.encode()
        except asyncio.TimeoutError: pass
    return buf


async def closed_code(ws, t=10):
    end = time.time() + t
    try:
        while time.time() < end: await asyncio.wait_for(ws.recv(), 1)
    except asyncio.TimeoutError: return await closed_code(ws, end - time.time()) if time.time() < end else "open"
    except websockets.ConnectionClosed as e: return e.rcvd.code if e.rcvd else "closed"
    return "open"


async def shut(*socks):
    for w in socks:
        try: await w.close()
        except Exception: pass
    await asyncio.sleep(1.5)


async def main():
    _, r = await dial(); rec("no credential refused", r in DENY, r)
    _, r = await dial("?ticket=junk"); rec("junk ticket refused", r in DENY, r)
    _, r = await dial("?token=junk"); rec("junk token refused", r in DENY, r)
    jar = lambda: aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
    async with jar() as s:
        rec("wrong password rejected", await login(s, "nope") == 401)
        _, r = await dial(f"?ticket={await ticket(s)}"); rec("no ticket without login", r in DENY, r)
    async with jar() as s:
        assert await login(s) == 200, "owner login failed"
        async with s.get(E.base + "/api/plugins/web-terminal/health") as h:
            body = await h.json(); rec("health ok after login", h.status == 200 and body.get("contract_ok"), body)
        _, r = await dial(f"?ticket={await ticket(s)}", origin="https://evil.example"); rec("foreign origin refused", r in DENY, r)
        _, r = await dial(f"?ticket={await ticket(s)}&profile=default"); rec("?profile= refused", r in DENY, r)

        t = await ticket(s); ws, r = await dial(f"?ticket={t}"); rec("owner gets a shell", r == "open", r)
        rec("shell round-trip", b"HELLO_42" in await run(ws, "echo HELLO_$((6*7))", "HELLO_42"))
        out = await run(ws, f"echo C=${{{CANARY}:-none}}=E\"N\"D", "=END")
        m = re.findall(rb"C=(\S+?)=END", out)
        rec("dashboard's own secrets not passed to shell", m[-1:] == [b"none"], [x[:7] for x in m])
        _, r = await dial(f"?ticket={t}"); rec("reused ticket refused", r in DENY, r)
        ws2, r2 = await dial(f"?ticket={await ticket(s)}"); rec("2nd shell allowed", r2 == "open", r2)
        _, r = await dial(f"?ticket={await ticket(s)}"); rec("3rd shell refused (limit 2)", r == 1013, r)
        await shut(ws, ws2)

        ws, _ = await dial(f"?ticket={await ticket(s)}")
        await ws.send(b"x" * 70000); rec("oversized frame closes (1009)", await closed_code(ws) == 1009)

        ws, _ = await dial(f"?ticket={await ticket(s)}"); drain = asyncio.create_task(closed_code(ws, 25))
        try:
            for _ in range(40): await asyncio.wait_for(ws.send(b"echo " + b"#" * 60000 + b"\r"), 5)
        except Exception: pass
        rec("input flood closes (1008)", await drain == 1008)

        ws, _ = await dial(f"?ticket={await ticket(s)}")
        await ws.send(b"\x1b[RESIZE:99999;99999]"); out = await run(ws, "stty size; echo SZ_D\"O\"NE", "SZ_DONE")
        m = re.findall(rb"(\d+) (\d+)\r?\nSZ_DONE", out)
        rec("absurd size clamped", bool(m) and int(m[-1][0]) <= 1000 and int(m[-1][1]) <= 2000, m)
        await ws.send(b"\x1b[RESIZE:-1;abc]\r")
        rec("garbage resize leaves shell alive", b"ALIVE_OK" in await run(ws, "echo ALIVE_O\"K\"", "ALIVE_OK"))
        pid = re.findall(rb"PID=(\d+)", await run(ws, "echo P\"I\"D=$$", "PID="))[-1].decode()
        await ws.send(b"exit\r"); rec("'exit' closes socket (4410)", await closed_code(ws) == 4410)
        await asyncio.sleep(1); rec("exited shell reaped", not os.path.exists(f"/proc/{pid}"), pid)

        ws, _ = await dial(f"?ticket={await ticket(s)}")
        p = re.findall(rb"PID=(\d+)", await run(ws, "echo P\"I\"D=$$", "PID="))[-1].decode()
        ws.transport.abort(); await asyncio.sleep(3)
        rec("abrupt disconnect kills shell", not os.path.exists(f"/proc/{p}"), p)

        E.write_cfg('["basic:someone-else"]', True)
        _, r = await dial(f"?ticket={await ticket(s)}"); rec("logged in but not owner refused", r in DENY, r)
        E.write_cfg("[]", True)
        _, r = await dial(f"?ticket={await ticket(s)}"); rec("empty owner list = nobody", r in DENY, r)
        E.write_cfg('["basic:owner"]', False)
        _, r = await dial(f"?ticket={await ticket(s)}"); rec("disabled plugin refuses at once", r in (4404, "http403", "http404"), r)
        E.write_cfg('["basic:owner"]', True)
    async with jar() as s:  # last: it trips the login rate limit
        codes = [await login(s, f"bad{i}") for i in range(30)]
        rec("password guessing rate-limited", 429 in codes)


if __name__ == "__main__":
    if not os.path.exists(PY): print(f"SKIP: no Hermes venv at {PY}"); sys.exit(77)
    try: socket.socket().bind((HOST, 0))
    except OSError: print(f"SKIP: {HOST} not a local address"); sys.exit(77)
    E = Env()
    try:
        if not E.start(): print(open(os.path.join(E.tmp, "dash.log")).read()[-2000:]); sys.exit("dashboard did not start")
        asyncio.run(main())
    finally:
        E.stop()
    f = sum(not o for _, o in R)
    print(f"---- {len(R) - f} passed, {f} failed"); sys.exit(1 if f else 0)
