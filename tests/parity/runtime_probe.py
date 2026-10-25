"""Runtime parity probe: log every hermesDesktop API the live app asks for that the
web bridge lacks, plus page errors, while visiting the main screens. Read-only."""
import json, sys, time
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:18090"
TRAP = r"""
(() => {
  window.__miss = {}; window.__errs = [];
  const wrap = (obj, pre) => new Proxy(obj, { get(t, k) {
    if (typeof k !== 'string' || k === 'then' || k.startsWith('__')) return t[k];
    const v = t[k];
    if (!(k in t)) { window.__miss[pre + k] = (window.__miss[pre + k] || 0) + 1; return v }
    if (!(k in t)) return v;
    return (v && typeof v === 'object' && !Array.isArray(v)) ? wrap(v, pre + k + '.') : v;
  }});
  let real;
  Object.defineProperty(window, 'hermesDesktop', { configurable: true,
    get() { return real }, set(v) { real = v && typeof v === 'object' ? wrap(v, '') : v } });
  addEventListener('error', e => window.__errs.push(String(e.message)));
  addEventListener('unhandledrejection', e => window.__errs.push('rej: ' + String(e.reason && e.reason.message || e.reason)));
})();
"""
ROUTES = ["/", "/#/settings?page=main", "/#/settings?page=appearance", "/#/settings?page=connections",
          "/#/settings?page=mcp", "/#/settings?page=plugins", "/#/skills", "/#/agents", "/#/cron", "/#/files", "/"]

with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    ctx = b.contexts[0]
    pg = ctx.new_page()
    console = []
    pg.on("console", lambda m: m.type in ("error", "warning") and console.append(m.text[:200]))
    pg.add_init_script(TRAP)
    try:
        for r in ROUTES:
            pg.goto(BASE + r) if r == "/" else pg.evaluate(f"location.hash = {json.dumps(r[1:])}")
            time.sleep(4)
        # try paste-image and Cmd+F style shortcuts
        pg.keyboard.press("Meta+f"); time.sleep(1)
        pg.keyboard.press("Control+f"); time.sleep(1)
        out = pg.evaluate("({miss: window.__miss, errs: window.__errs, title: document.title})")
        out["console"] = sorted(set(console))[:60]
        print(json.dumps(out, indent=1))
    finally:
        pg.close()
