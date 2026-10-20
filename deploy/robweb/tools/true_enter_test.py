#!/usr/bin/env python3
"""True-Enter acceptance test for the Desktop Web embedded browser pane.

Talks to Chromium's DevTools port directly (no harness relay), so every key is
delivered through Input.dispatchKeyEvent -- the browser's native, trusted input
route -- not a synthetic DOM KeyboardEvent.

Flow:
  1. attach to the Desktop Web tab (first page target matching --match)
  2. seed the chat composer with a sentinel draft (via insertText after a real click)
  3. load --url in the pane, locate --field inside the remote page
  4. real mouse click on the field (page-coords -> displayed <img> rect)
  5. real keystrokes for --text, then a real Enter (rawKeyDown + char + keyUp)
  6. verify remote page navigated/changed, composer draft unchanged
  7. print a JSON report naming which layer failed, if any

Scope: tests the pane plumbing from the SERVER's automation Chromium on loopback. It does
NOT exercise the public Cloudflare-Access path or a real device/keyboard; keep a manual
keypress as the final acceptance check. Restores draft text, draft storage and pane URL
on exit (success or failure). Default page is httpbin's echo form so the POSTed field
value can be asserted, not just a URL change.

Usage:
  python3 true_enter_test.py --port 9222 --match '127.0.0.1:18090/?profile=hermes-admin' \
      --url https://en.wikipedia.org/wiki/Special:Search --field 'input[name=search]' --text Hermes
"""
import argparse, asyncio, json, sys, time, urllib.request
import websockets

class CDP:
    def __init__(self, ws): self.ws, self.i, self.pending, self.events = ws, 0, {}, []
    async def pump(self):
        async for raw in self.ws:
            m = json.loads(raw)
            if "id" in m and m["id"] in self.pending: self.pending.pop(m["id"]).set_result(m)
            else: self.events.append(m)
    async def send(self, method, params=None, timeout=20):
        self.i += 1; fut = asyncio.get_event_loop().create_future(); self.pending[self.i] = fut
        await self.ws.send(json.dumps({"id": self.i, "method": method, "params": params or {}}))
        m = await asyncio.wait_for(fut, timeout)
        if "error" in m: raise RuntimeError(f"{method}: {m['error']}")
        return m.get("result", {})
    async def eval(self, expr, timeout=20):
        r = await self.send("Runtime.evaluate", {"expression": expr, "awaitPromise": True, "returnByValue": True}, timeout)
        if "exceptionDetails" in r: raise RuntimeError(r["exceptionDetails"].get("text") or json.dumps(r["exceptionDetails"])[:300])
        return r["result"].get("value")

def pick_target(port, match):
    tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5))
    hits = [t for t in tabs if t["type"] == "page" and match in t["url"]]
    if len(hits) != 1:
        raise SystemExit(f"need exactly one page target containing {match!r}, found {len(hits)}: {[t['url'][:70] for t in hits] or [t['url'][:60] for t in tabs if t['type']=='page']}")
    return hits[0]

REMOTE = "(async()=>{const v=document.querySelector('webview img').__hermesPreviewView;return JSON.stringify(await v._call({op:'eval',code:%s}))})()"
async def remote(c, code):
    v = await c.eval(REMOTE % json.dumps(code))
    try: v = json.loads(v)
    except Exception: pass
    if isinstance(v, str):
        try: v = json.loads(v)
        except Exception: pass
    return v

async def key(c, k, code, vk, text=None, log=None):
    base = {"key": k, "code": code, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
    seq = [dict(type="rawKeyDown", **base)] if text is None or k == "Enter" else [dict(type="keyDown", **base)]
    if text: seq.append(dict(type="char", text=text, unmodifiedText=text, **base))
    seq.append(dict(type="keyUp", **base))
    for p in seq:
        await c.send("Input.dispatchKeyEvent", p)
        if log is not None: log.append(p)

async def main(a):
    t = pick_target(a.port, a.match)
    report = {"target": t["url"], "key_events": [], "layer_failed": None}
    async with websockets.connect(t["webSocketDebuggerUrl"], max_size=50_000_000) as ws:
        c = CDP(ws); pump = asyncio.create_task(c.pump())
        await c.send("Runtime.enable"); await c.send("Page.enable")
        # snapshot user state so it can be restored afterwards (draft text, draft storage, pane URL)
        saved = await c.eval("(()=>({draft:document.querySelector('[role=textbox][aria-label=Message]')?.textContent??null,store:localStorage.getItem('hermes:composer-drafts:v3'),pane:document.querySelector('webview')?.getAttribute('src')??null}))()")
        report["restored"] = None
        try:
            return await _run(c, a, report, saved)
        finally:
            try:
                await c.eval(f"(()=>{{const e=document.querySelector('[role=textbox][aria-label=Message]');if(e)e.textContent={json.dumps(saved.get('draft') or '')};"
                             f"const s={json.dumps(saved.get('store'))};if(s===null)localStorage.removeItem('hermes:composer-drafts:v3');else localStorage.setItem('hermes:composer-drafts:v3',s);"
                             f"const p={json.dumps(saved.get('pane'))};const w=document.querySelector('webview');if(w&&p&&w.getAttribute('src')!==p)w.loadURL(p);return 1}})()")
                report["restored"] = {"draft": saved.get("draft"), "pane": saved.get("pane")}
            except Exception as e: report["restored"] = f"restore failed: {e}"[:200]
            pump.cancel()

async def _run(c, a, report, saved):
        await c.eval("(()=>{localStorage.removeItem('hermes:composer-drafts:v3');return 1})()")
        # 1. sentinel draft in the composer via a real click + insertText
        ed = await c.eval("(()=>{const e=document.querySelector('[role=textbox][aria-label=Message]');if(!e)return null;e.textContent='';const r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()")
        if not ed: report["layer_failed"] = "app: no chat composer found"; return report
        sentinel = f"DRAFT-{int(time.time())%100000}"
        for typ in ("mousePressed", "mouseReleased"):
            await c.send("Input.dispatchMouseEvent", {"type": typ, "x": ed["x"], "y": ed["y"], "button": "left", "clickCount": 1})
        await c.send("Input.insertText", {"text": sentinel}); await asyncio.sleep(0.3)
        draft = lambda: c.eval("document.querySelector('[role=textbox][aria-label=Message]')?.textContent||''")
        report["draft_before"] = await draft()
        # 2. pane present + load url
        pane = await c.eval("(()=>{const w=document.querySelector('webview');if(!w)return null;const i=w.querySelector('img');const r=i?i.getBoundingClientRect():null;return {hasImg:!!i,rect:r&&{x:r.x,y:r.y,w:r.width,h:r.height}}})()")
        if not pane or not pane["hasImg"] or not pane["rect"] or pane["rect"]["w"] < 10:
            report["layer_failed"] = "ui: preview pane missing or hidden (0x0)"; report["pane"] = pane; return report
        await c.eval(f"(()=>{{document.querySelector('webview').loadURL({json.dumps(a.url)});return 1}})()")
        for _ in range(30):
            await asyncio.sleep(1)
            try:
                st = await remote(c, f"JSON.stringify({{ready:document.readyState,has:!!document.querySelector({json.dumps(a.field)}),url:location.href}})")
                if isinstance(st, dict) and st.get("has"): break
            except Exception as e: st = {"err": str(e)[:120]}
        report["page_before"] = st
        if not (isinstance(st, dict) and st.get("has")):
            report["layer_failed"] = "relay/page: field never appeared"; return report
        # 3. click the field: page coords -> displayed img rect
        box = await remote(c, f"(()=>{{const b=document.querySelector({json.dumps(a.field)}).getBoundingClientRect();return JSON.stringify({{x:b.x+b.width/2,y:b.y+b.height/2,vw:innerWidth,vh:innerHeight}})}})()")
        r = pane["rect"]; sx = r["x"] + r["w"] * box["x"] / box["vw"]; sy = r["y"] + r["h"] * box["y"] / box["vh"]
        hit = await c.eval(f"document.elementFromPoint({sx},{sy})?.tagName")
        report["click"] = {"x": round(sx), "y": round(sy), "hit": hit}
        if hit != "IMG": report["layer_failed"] = "ui: click point does not land on the pane image"; return report
        await c.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": sx, "y": sy})
        for typ in ("mousePressed", "mouseReleased"):
            await c.send("Input.dispatchMouseEvent", {"type": typ, "x": sx, "y": sy, "button": "left", "clickCount": 1})
        await asyncio.sleep(0.8)
        report["focus"] = {"local": await c.eval("document.activeElement.tagName"),
                           "remote": await remote(c, "document.activeElement&&(document.activeElement.name||document.activeElement.tagName)")}
        if report["focus"]["local"] != "IMG": report["layer_failed"] = "ui: pane image did not take focus"; return report
        if not report["focus"]["remote"] or report["focus"]["remote"] == "BODY":
            report["layer_failed"] = "relay: click reached pane but remote page field not focused"; return report
        # 4. real typing
        for ch in a.text:
            vk = ord(ch.upper()); await key(c, ch, f"Key{ch.upper()}" if ch.isalpha() else "", vk, text=ch, log=report["key_events"])
        await asyncio.sleep(0.5)
        report["remote_value"] = await remote(c, f"(document.querySelector({json.dumps(a.field)})||{{}}).value")
        report["draft_after_typing"] = await draft()
        if report["remote_value"] != a.text:
            report["layer_failed"] = "relay/CDP: printable keys did not reach the remote field"; return report
        if report["draft_after_typing"] != sentinel:
            report["layer_failed"] = "isolation: pane keystrokes leaked into chat composer"; return report
        # 5. true Enter -- first prove the page has NOT moved yet (so only Enter can explain a change)
        pre = await remote(c, "location.href")
        report["url_before_enter"] = pre
        if pre != st.get("url"):
            report["layer_failed"] = "page: URL changed before Enter was sent (click/typing or page script navigated)"; return report
        await key(c, "Enter", "Enter", 13, text="\r", log=report["key_events"])
        changed = None
        for _ in range(15):
            await asyncio.sleep(1)
            try:
                st2 = await remote(c, "JSON.stringify({url:location.href,title:document.title,ready:document.readyState})")
                if isinstance(st2, dict) and st2.get("url") != st.get("url"): changed = st2; break
            except Exception: pass
        report["page_after"] = changed or st2
        report["draft_after_enter"] = await draft()
        report["enter_submitted"] = bool(changed)
        report["composer_isolated"] = report["draft_after_enter"] == sentinel
        if changed:
            try: report["posted_form"] = await remote(c, "(()=>{try{return JSON.parse(document.body.innerText).form}catch(e){return null}})()")
            except Exception: report["posted_form"] = None
        if not changed: report["layer_failed"] = "CDP/page: Enter delivered (events logged) but remote page did not navigate"
        elif not report["composer_isolated"]: report["layer_failed"] = "isolation: Enter altered the chat composer"
        elif isinstance(report.get("posted_form"), dict) and report["posted_form"].get(a.field.split("=")[-1].strip("]")) != a.text:
            report["layer_failed"] = "page: navigated but the submitted form does not contain the typed text"
        return report

if __name__ == "__main__":
    p = argparse.ArgumentParser(); p.add_argument("--port", type=int, default=9222); p.add_argument("--match", default="127.0.0.1:18090/?profile=hermes-admin&kc=2")
    p.add_argument("--url", default="https://httpbin.org/forms/post"); p.add_argument("--field", default="input[name=custname]"); p.add_argument("--text", default="Hermes")
    rep = asyncio.run(main(p.parse_args()))
    print(json.dumps(rep, indent=1)); sys.exit(0 if rep.get("layer_failed") is None else 2)
