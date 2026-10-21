#!/usr/bin/env python3
"""Real-browser tests for Desktop Web.

Drives a real Chromium that is ALREADY signed in to the deployment (it attaches
over the DevTools port, opens its own tab, and closes it afterwards). Every
click and keypress is real browser input. User settings it touches (theme,
active profile) are restored at the end, pass or fail.

Usage:
  python3 tests/browser/run_browser_tests.py                 # safe set
  python3 tests/browser/run_browser_tests.py --send          # + real chat round-trip (uses a model call)
  python3 tests/browser/run_browser_tests.py -k profile      # only tests whose name contains "profile"
Options: --cdp http://127.0.0.1:9222  --base http://127.0.0.1:18090
         --profiles hermes-admin,research   --send-profile cheap-executor
Exit code 0 = all passed. Failure screenshots go to tests/browser/artifacts/.
Needs: python3 + playwright (pip install playwright). No pytest required.
"""
import argparse, json, os, re, sys, time, traceback
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

HERE = os.path.dirname(os.path.abspath(__file__))
ART = os.path.join(HERE, "artifacts")
COMPOSER = '[role=textbox][aria-label=Message]'
BAD_TEXT = re.compile(r"requires a ws:// or wss:// URL|Failed to switch to profile|Something went wrong", re.I)
TESTS = []

def test(fn):
    TESTS.append(fn); return fn

class Fail(AssertionError): pass

def check(cond, msg):
    if not cond: raise Fail(msg)

class App:
    """One fresh tab on the deployment + collectors for page crashes."""
    def __init__(self, ctx, base):
        self.ctx, self.base = ctx, base
        self.page = ctx.new_page()
        self.errors, self.loads = [], 0
        self.page.on("pageerror", lambda e: self.errors.append(str(e)[:300]))
        # evidence for intermittent "Failed to fetch": which requests failed, and how
        self.failed = []
        self.page.on("requestfailed", lambda r: self.failed.append(f"{r.method} {r.url[:120]} {r.failure}"))
        self.page.on("load", lambda *_: setattr(self, "loads", self.loads + 1))

    def open(self, path="/"):
        self.page.goto(self.base + path)
        self.page.wait_for_selector(COMPOSER, timeout=45000)
        time.sleep(1.5)

    def text(self):
        return self.page.evaluate("document.body.innerText")

    def assert_clean(self, where):
        bad = BAD_TEXT.search(self.text())
        check(not bad, f"{where}: error shown on screen: {bad.group(0) if bad else ''!r}")
        check(not self.errors, f"{where}: page crashed: {self.errors[:2]} failed requests: {self.failed[-5:]}")

    def profile_menu(self):
        self.page.get_by_role("button", name="Profiles", exact=True).click()
        self.page.wait_for_selector("[role=menuitemradio]", timeout=5000)

    def active_profile(self):
        self.profile_menu()
        name = self.page.evaluate("""(()=>{const e=[...document.querySelectorAll('[role=menuitemradio]')]
            .find(e=>e.getAttribute('aria-checked')==='true'||e.dataset.state==='checked');return e?e.innerText:null})()""")
        self.page.keyboard.press("Escape")
        return name

    def switch_profile(self, name):
        self.profile_menu()
        items = self.page.locator("[role=menuitemradio]")
        target = None
        for i in range(items.count()):
            lines = [l.strip() for l in items.nth(i).inner_text().splitlines()]
            if name in lines:
                target = items.nth(i); break
        check(target is not None, f"profile {name!r} not in the Profiles menu")
        target.click()
        time.sleep(float(os.environ.get("SWITCH_WAIT","2.5")))
        self.page.wait_for_selector(COMPOSER, timeout=30000)

    def close(self):
        try: self.page.close()
        except Exception: pass

# ── Tests ────────────────────────────────────────────────────────────────

@test
def boot_loads_clean(app, a):
    app.open("/")
    app.assert_clean("first load")
    check(app.page.evaluate("typeof window.hermesDesktop?.getGatewayWsUrl") == "function", "web bridge not installed")

@test
def profile_switch_via_menu(app, a):
    app.open("/")
    for name in a.profiles + a.profiles[:1]:
        app.switch_profile(name)
        app.assert_clean(f"after switching to {name}")
        got = app.active_profile() or ""
        check(name in [l.strip() for l in got.splitlines()], f"menu shows {got!r} as active, expected {name!r}")

@test
def profile_switch_rapid(app, a):
    app.open("/")
    seq = (a.profiles * 3)[:5]
    for name in seq:
        app.switch_profile(name)
    time.sleep(4)
    app.assert_clean("after 5 fast profile switches")
    got = app.active_profile() or ""
    check(seq[-1] in [l.strip() for l in got.splitlines()], f"ended on {got!r}, expected {seq[-1]!r}")

@test
def profile_deep_link(app, a):
    for name in a.profiles:
        app.open(f"/?profile={name}")
        app.assert_clean(f"open /?profile={name}")

@test
def unknown_profile_deep_link_is_harmless(app, a):
    app.page.goto(a.base + "/?profile=..%2F..%2Fetc")
    app.page.wait_for_selector(COMPOSER, timeout=45000)
    time.sleep(2)
    check(not app.errors, f"page crashed: {app.errors[:2]}")

@test
def theme_switching(app, a):
    app.page.goto(a.base + "/#/settings?page=main")
    app.page.get_by_role("button", name="Expand: Appearance").wait_for(timeout=45000)
    app.page.get_by_role("button", name="Expand: Appearance").click()
    app.page.get_by_role("button", name="Theme", exact=True).click()
    for mode in ("Light", "Dark", "System", "Dark"):
        app.page.get_by_role("button", name=mode, exact=True).click(); time.sleep(0.8)
        if mode != "System":
            got = app.page.evaluate("document.documentElement.dataset.hermesMode")
            check(got == mode.lower(), f"after {mode}: page mode is {got!r}")
    for palette in ("Midnight", "Mono", "Nous"):
        app.page.locator("main button", has_text=re.compile("^" + palette)).first.click(); time.sleep(0.8)
    app.assert_clean("after theme changes")

@test
def attachment_picker(app, a):
    app.open("/")
    sample = os.path.join(ART, "attach-sample.txt"); os.makedirs(ART, exist_ok=True)
    open(sample, "w").write("desktop-web attachment test\n")
    app.page.get_by_role("button", name="Add context").click(); time.sleep(0.8)
    item = app.page.get_by_role("menuitem", name=re.compile("file|upload|attach", re.I)).first
    with app.page.expect_file_chooser(timeout=8000) as fc:
        item.click()
    fc.value.set_files(sample); time.sleep(1.5)
    check("attach-sample.txt" in app.text(), "attached file name not shown in the composer")
    app.assert_clean("after attaching")
    # remove the attachment again so no draft is left behind
    rm = app.page.get_by_role("button", name=re.compile("remove|delete", re.I))
    if rm.count(): rm.first.click()

@test
def expired_signin_reloads_to_sign_in(app, a):
    """Simulates Cloudflare Access expiry in a real browser: the ticket request
    fails and the server redirects away. The page must reload (to show the
    sign-in page), never show the old ws:// error, and never loop."""
    app.open("/")
    app.page.evaluate("sessionStorage.removeItem('hermes.web.reauthAt')")
    app.page.route("**/api/auth/ws-ticket", lambda r: r.abort("failed"))
    app.page.route("**/api/status", lambda r: r.fulfill(status=302, headers={"Location": "https://access.invalid/login"}))
    app.page.evaluate("window.__suiteMark = 1")
    with app.page.expect_event("load", timeout=15000):
        app.page.evaluate("void window.hermesDesktop.getGatewayWsUrl().catch(()=>{})")
    app.page.wait_for_selector(COMPOSER, timeout=45000)
    check(app.page.evaluate("window.__suiteMark") is None, "page did not reload")
    # second expiry right after: guard must stop a reload loop
    app.page.evaluate("window.__suiteMark = 2")
    app.page.evaluate("window.hermesDesktop.getGatewayWsUrl().catch(()=>{})")
    time.sleep(4)
    check(app.page.evaluate("window.__suiteMark") == 2, "reloaded again within a minute (reload loop)")
    check(not re.search("requires a ws://", app.text()), "old ws:// error shown")
    check(app.page.locator("#hermes-web-signin").is_visible(), "no 'sign-in expired' message shown on second expiry")
    app.page.evaluate("document.getElementById('hermes-web-signin')?.remove()")
    app.page.unroute("**/api/auth/ws-ticket"); app.page.unroute("**/api/status")

@test
def ticket_server_error_does_not_reload(app, a):
    app.open("/")
    app.page.route("**/api/auth/ws-ticket", lambda r: r.fulfill(status=500, body="boom"))
    before = app.loads
    err = app.page.evaluate("window.hermesDesktop.getGatewayWsUrl().then(u=>'URL:'+u, e=>String(e))")
    check(err.startswith("Error: 500"), f"unexpected result {err!r}")
    time.sleep(2)
    check(app.loads == before, "page reloaded on a plain server error")
    app.page.unroute("**/api/auth/ws-ticket")

@test
def every_connection_uses_a_fresh_ticket(app, a):
    app.open("/")
    seen = []
    app.page.on("request", lambda r: "/api/auth/ws-ticket" in r.url and seen.append(r.url))
    urls = app.page.evaluate("Promise.all([1,2,3].map(()=>window.hermesDesktop.getGatewayWsUrl()))")
    check(len(set(urls)) == 3, "two connections got the same ticket")
    check(len(seen) == 3, f"expected 3 ticket requests, saw {len(seen)}")
    check(all(u.startswith(("ws://", "wss://")) and "ticket=" in u for u in urls), f"bad ws URL {urls[0]!r}")

@test
def chat_round_trip(app, a):
    if not a.send: return "skipped (use --send)"
    app.open(f"/?profile={a.send_profile}")
    token = f"SUITE-OK-{int(time.time()) % 100000}"
    app.page.get_by_role("button", name=re.compile("^New session")).first.click(); time.sleep(1)
    app.page.click(COMPOSER)
    app.page.keyboard.type(f"Reply with exactly: {token}")
    app.page.keyboard.press("Enter")
    deadline = time.time() + 120
    while time.time() < deadline:
        if app.text().count(token) >= 2: break
        time.sleep(2)
    check(app.text().count(token) >= 2, f"no reply containing {token} within 120s")
    app.assert_clean("after chat reply")

# ── Runner ───────────────────────────────────────────────────────────────

def snapshot_settings(page):
    return page.evaluate("""JSON.stringify(Object.fromEntries(['hermes-desktop-mode-v1','hermes-desktop-theme-v2',
        'hermes-desktop-active-profile-v1','hermes.desktop.lastProfileByConnection','hermes-desktop-profile-themes-v1',
        'hermes-desktop-profile-modes-v1'].map(k=>[k,localStorage.getItem(k)])))""")

def restore_settings(page, snap):
    page.evaluate("""s=>{for(const [k,v] of Object.entries(JSON.parse(s))){if(v===null)localStorage.removeItem(k);else localStorage.setItem(k,v)}}""", snap)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp", default="http://127.0.0.1:9222")
    ap.add_argument("--base", default="http://127.0.0.1:18090")
    ap.add_argument("--profiles", default="hermes-admin,research")
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--send-profile", default="cheap-executor")
    ap.add_argument("-k", default="")
    a = ap.parse_args(); a.profiles = a.profiles.split(",")
    os.makedirs(ART, exist_ok=True)
    results = []
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(a.cdp)
        ctx = browser.contexts[0]
        guard = App(ctx, a.base); guard.open("/")
        snap = snapshot_settings(guard.page)
        try:
            for fn in TESTS:
                if a.k and a.k not in fn.__name__: continue
                app = App(ctx, a.base); t0 = time.time()
                try:
                    note = fn(app, a)
                    results.append((fn.__name__, "SKIP" if note else "PASS", note or "", time.time() - t0))
                except Exception as e:
                    shot = os.path.join(ART, f"{fn.__name__}.png")
                    try: app.page.screenshot(path=shot)
                    except Exception: shot = "(no screenshot)"
                    msg = str(e) if isinstance(e, (Fail, PWTimeout)) else traceback.format_exc(limit=2)
                    results.append((fn.__name__, "FAIL", f"{msg.strip()[:400]} [{shot}]", time.time() - t0))
                finally:
                    app.close()
        finally:
            restore_settings(guard.page, snap); guard.page.reload(); guard.close()
    for name, st, note, dt in results:
        print(f"{st:4}  {name:42} {dt:5.1f}s  {note}")
    fails = sum(r[1] == "FAIL" for r in results)
    print(f"---- {sum(r[1]=='PASS' for r in results)} passed, {fails} failed, {sum(r[1]=='SKIP' for r in results)} skipped")
    with open(os.path.join(ART, "last-run.json"), "w") as f:
        json.dump([dict(zip(("test", "status", "note", "seconds"), r)) for r in results], f, indent=1)
    sys.exit(1 if fails else 0)

if __name__ == "__main__":
    main()
