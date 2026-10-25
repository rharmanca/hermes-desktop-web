"""Real-browser checks for the desktop-parity bridge features.

Runs against any deployment (default: staging on :18190) through the server's
Chromium (CDP :9222). Opens and closes its own tabs, restores localStorage and
permissions. Does not send chats.

  python3 tests/browser/parity_browser_checks.py [--base http://127.0.0.1:18190]
"""
import argparse, json, sys, time
sys.path.insert(0, __file__.rsplit("/", 1)[0])
from run_browser_tests import App, check, Fail  # noqa: E402
from playwright.sync_api import sync_playwright

PNG_JS = """async () => {
  const c = document.createElement('canvas'); c.width = 8; c.height = 8
  c.getContext('2d').fillRect(0, 0, 8, 8)
  const blob = await new Promise(r => c.toBlob(r, 'image/png'))
  await navigator.clipboard.write([new ClipboardItem({'image/png': blob})])
  return blob.size
}"""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:18190")
    ap.add_argument("--cdp", default="http://127.0.0.1:9222")
    a = ap.parse_args()
    results = []

    def run(name, fn):
        try:
            fn(); results.append((name, "PASS", ""))
        except Exception as e:  # noqa: BLE001
            results.append((name, "FAIL", str(e)[:300]))

    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(a.cdp)
        ctx = b.contexts[0]
        app = App(ctx, a.base)
        saved = None
        try:
            app.open("/")
            saved = app.page.evaluate("JSON.stringify(Object.fromEntries(Object.entries(localStorage).filter(([k])=>k.startsWith('hermes.web.'))))")

            def bridge_is_new():
                check(app.page.evaluate("typeof window.hermesDesktop.profile.remember === 'function' && !!window.hermesDesktop.saveClipboardImage.toString().includes('clipboard')"),
                      "deployment is not serving the parity bridge")

            def profile_reload():
                app.switch_profile("research"); time.sleep(3)
                app.page.reload(); app.page.wait_for_selector("textarea, [contenteditable=true]", timeout=45000); time.sleep(4)
                check("research" in (app.active_profile() or ""), f"after reload active={app.active_profile()!r}")
                check("profile=research" in app.page.url, f"url={app.page.url}")
                app.assert_clean("profile reload")

            def default_beats_last():
                app.page.evaluate("localStorage.setItem('hermes.web.defaultProfile', JSON.stringify({connectionId:null, profile:'ops'}))")
                app.page.goto(a.base + "/"); app.page.wait_for_selector("textarea, [contenteditable=true]", timeout=45000); time.sleep(4)
                check("profile=ops" in app.page.url, f"url={app.page.url}")
                app.assert_clean("default profile")

            def tampered_storage():
                app.page.evaluate("localStorage.removeItem('hermes.web.defaultProfile'); localStorage.setItem('hermes.web.lastProfile','../../etc')")
                app.page.goto(a.base + "/"); time.sleep(6)
                check("profile=" not in app.page.url, f"tampered value reached the URL: {app.page.url}")
                check(not app.errors, f"page crashed: {app.errors[:2]}")

            def notification_click():
                ctx.grant_permissions(["notifications"], origin=a.base)
                out = app.page.evaluate("""async () => {
                  const got = []; const off = window.hermesDesktop.onFocusSession(id => got.push(id))
                  const ok = await window.hermesDesktop.notify({title:'parity test', body:'x', sessionId:'probe-1'})
                  return {ok, perm: Notification.permission}
                }""")
                check(out["ok"] is True and out["perm"] == "granted", f"notify: {out}")

            def notification_no_permission():
                ctx.clear_permissions()
                out = app.page.evaluate("window.hermesDesktop.notify({title:'should not show'})")
                check(out is False, f"notify without permission returned {out!r}")

            def new_tab():
                before = len(ctx.pages)
                with ctx.expect_page(timeout=15000) as info:
                    r = app.page.evaluate("window.hermesDesktop.openWindow({connectionId:null, profile:'research'})")
                tab = info.value
                tab.wait_for_load_state(); time.sleep(5)
                check(r.get("ok") is True, f"result {r}")
                check(tab.url.startswith(a.base + "/?peer=1&profile=research"), f"tab url {tab.url}")
                errs = []
                tab.on("pageerror", lambda e: errs.append(str(e)))
                time.sleep(3)
                body = tab.evaluate("document.body.innerText")
                tab.close()
                check(len(ctx.pages) == before, "extra tab left open")
                check(not errs and "Something went wrong" not in body, f"new tab broken: {errs[:2]}")

            def session_tab():
                with ctx.expect_page(timeout=15000) as info:
                    app.page.evaluate("window.hermesDesktop.openSessionWindow('does-not-exist-123', {profile:'research'})")
                tab = info.value; tab.wait_for_load_state(); time.sleep(5)
                url = tab.url; body = tab.evaluate("document.body.innerText"); tab.close()
                check("win=secondary" in url and url.endswith("#/does-not-exist-123"), f"url {url}")
                check("Something went wrong" not in body, "pop-out crashed on unknown session")

            def paste_image():
                ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin=a.base)
                app.page.bring_to_front()
                size = app.page.evaluate(PNG_JS)
                path = app.page.evaluate("window.hermesDesktop.saveClipboardImage()")
                check(path.startswith("/browser-upload/") and path.endswith(".png"), f"path {path!r}")
                data = app.page.evaluate(f"window.hermesDesktop.readFileDataUrlForAttach({json.dumps(path)})")
                check(data and data.startswith("data:image/png;base64,"), "bytes not readable")
                app.page.evaluate("navigator.clipboard.writeText('just text')")
                check(app.page.evaluate("window.hermesDesktop.saveClipboardImage()") == "", "text clipboard should give ''")
                ctx.clear_permissions()

            for n, f in [("bridge served", bridge_is_new), ("profile remembered across reload", profile_reload),
                         ("saved default beats last-used", default_beats_last), ("tampered storage ignored", tampered_storage),
                         ("notification shows when allowed", notification_click), ("no notification without permission", notification_no_permission),
                         ("new window opens a working tab", new_tab), ("session pop-out tab", session_tab),
                         ("paste image from clipboard", paste_image)]:
                run(n, f)
        finally:
            try:
                app.page.evaluate("(s) => { for (const k of Object.keys(localStorage)) if (k.startsWith('hermes.web.')) localStorage.removeItem(k); for (const [k,v] of Object.entries(JSON.parse(s||'{}'))) localStorage.setItem(k,v) }", saved)
            except Exception:  # noqa: BLE001
                pass
            ctx.clear_permissions()
            app.page.close()

    for n, s, d in results:
        print(f"{s}  {n}" + (f"  -- {d}" if d else ""))
    fails = sum(1 for _, s, _ in results if s == "FAIL")
    print(f"---- {len(results) - fails} passed, {fails} failed")
    sys.exit(1 if fails else 0)

if __name__ == "__main__":
    main()
