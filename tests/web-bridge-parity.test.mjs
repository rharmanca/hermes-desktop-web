// Desktop-parity features of web-bridge.js: remembered/default profile,
// browser notifications, "open in new window", and paste-image.
// Run with: node --test tests/*.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

const SRC = readFileSync(new URL('../web-bridge.js', import.meta.url), 'utf8')

function storage(init = {}) {
  const m = new Map(Object.entries(init))
  return { getItem: k => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: k => m.delete(k), _m: m }
}

function load({ search = '', hash = '#/', local = {}, permission = 'default', clipboard, openReturns = {}, timers = setTimeout } = {}) {
  const listeners = {}
  const opened = []
  const notes = []
  const location = { protocol: 'https:', host: 'd.example', origin: 'https://d.example', pathname: '/', search, hash }
  Object.defineProperty(location, 'href', { get: () => location.origin + location.pathname + location.search + location.hash })
  const history = {
    state: null, calls: 0,
    replaceState(_s, _t, url) {
      this.calls++
      const u = new URL(url, location.origin)
      location.pathname = u.pathname; location.search = u.search; location.hash = u.hash
    }
  }
  class FakeNotification {
    static permission = permission
    static asked = 0
    static requestPermission() { FakeNotification.asked++; return Promise.resolve('granted') }
    constructor(title, opts) { this.title = title; this.opts = opts; this.closed = false; notes.push(this) }
    close() { this.closed = true }
  }
  const window = {
    open: (url, target) => { opened.push({ url, target }); return openReturns === null ? null : { opener: 'x' } },
    focus: () => { window.focused = (window.focused || 0) + 1 }
  }
  const ctx = {
    window, location, history, localStorage: storage(local), sessionStorage: storage(),
    URL, URLSearchParams, Blob, FormData, TextDecoder, setTimeout: timers, clearTimeout, console, Promise,
    fetch: async () => ({ ok: true, status: 200, headers: { get: () => 'application/json' }, text: async () => '{}', json: async () => ({}) }),
    navigator: { userAgent: 'test', clipboard }, crypto: globalThis.crypto, Notification: FakeNotification,
    addEventListener: (t, f) => { (listeners[t] ||= new Set()).add(f) },
    removeEventListener: (t, f) => { listeners[t]?.delete(f) },
    FileReader: class {
      readAsDataURL(blob) {
        blob.arrayBuffer().then(b => { this.result = `data:${blob.type};base64,${Buffer.from(b).toString('base64')}`; this.onload() })
      }
    }
  }
  vm.createContext(ctx)
  vm.runInContext(SRC, ctx, { filename: 'web-bridge.js' })
  const fire = t => [...(listeners[t] || [])].forEach(f => f({}))
  return { bridge: window.hermesDesktop, location, history, ctx, opened, notes, fire, FakeNotification, window }
}

// ── Remembered / default profile ─────────────────────────────────────────

test('plain visit boots on the last profile used in this browser', () => {
  const { location } = load({ local: { 'hermes.web.lastProfile': 'research' } })
  assert.equal(new URLSearchParams(location.search).get('profile'), 'research')
  assert.equal(location.hash, '#/', 'route is preserved')
})

test('saved default beats last-used', () => {
  const { location } = load({ local: { 'hermes.web.lastProfile': 'research', 'hermes.web.defaultProfile': '{"connectionId":null,"profile":"ops"}' } })
  assert.equal(new URLSearchParams(location.search).get('profile'), 'ops')
})

test('explicit ?profile= in the link always wins', () => {
  const { location, history } = load({ search: '?profile=health', local: { 'hermes.web.lastProfile': 'research' } })
  assert.equal(new URLSearchParams(location.search).get('profile'), 'health')
  assert.equal(history.calls, 0)
})

test('pop-out and peer tabs are never redirected', () => {
  for (const search of ['?win=secondary', '?peer=1']) {
    const { location } = load({ search, local: { 'hermes.web.lastProfile': 'research' } })
    assert.equal(new URLSearchParams(location.search).get('profile'), null, search)
  }
})

test('tampered stored values are ignored (traversal, script, junk, oversize)', () => {
  const bad = ['../../etc', '..', 'a/b', '<script>', 'x y', '', ' ', '-lead', 'a'.repeat(65), 'a..b', 'ok?x=1', 'ok#h']
  for (const v of bad) {
    const { location } = load({ local: { 'hermes.web.lastProfile': v } })
    assert.equal(new URLSearchParams(location.search).get('profile'), null, JSON.stringify(v))
  }
  for (const v of ['not json', '{"profile":"../x"}', '{"profile":42}', 'null', '[]']) {
    const { location } = load({ local: { 'hermes.web.defaultProfile': v } })
    assert.equal(new URLSearchParams(location.search).get('profile'), null, v)
  }
})

test('remember() stores the profile and updates the address bar, keeping the route', async () => {
  const { bridge, location, ctx } = load({ hash: '#/abc' })
  assert.deepEqual({ ...(await bridge.profile.remember('research')) }, { profile: 'research' })
  assert.equal(ctx.localStorage.getItem('hermes.web.lastProfile'), 'research')
  assert.equal(new URLSearchParams(location.search).get('profile'), 'research')
  assert.equal(location.hash, '#/abc')
  assert.equal((await bridge.profile.get()).profile, 'research')
})

test('remember() rejects bad names without touching storage or URL', async () => {
  const { bridge, location, ctx } = load()
  for (const v of ['../x', null, undefined, 42, {}, 'a b']) await bridge.profile.remember(v)
  assert.equal(ctx.localStorage.getItem('hermes.web.lastProfile'), null)
  assert.equal(location.search, '')
})

test('remember() in a pop-out tab saves preference but leaves its URL alone', async () => {
  const { bridge, location } = load({ search: '?win=secondary' })
  await bridge.profile.remember('research')
  assert.equal(location.search, '?win=secondary')
})

test('setDefault/getDefault round-trip and notify subscribers; bad input throws', async () => {
  const { bridge } = load()
  const seen = []
  const off = bridge.profile.onDefaultChanged(r => seen.push(r.profile))
  const saved = await bridge.profile.setDefault({ connectionId: null, profile: 'ops' })
  assert.equal(saved.profile, 'ops')
  assert.equal((await bridge.profile.getDefault()).profile, 'ops')
  off()
  await bridge.profile.setDefault({ connectionId: null, profile: 'research' })
  assert.deepEqual(seen, ['ops'], 'unsubscribe works')
  await assert.rejects(bridge.profile.setDefault({ profile: '../evil' }))
  await assert.rejects(bridge.profile.setDefault(null))
  assert.equal(bridge.profile.onDefaultChanged('not a fn')(), undefined)
})

// ── Notifications ────────────────────────────────────────────────────────

test('notify returns false and shows nothing without permission', async () => {
  const { bridge, notes } = load({ permission: 'default' })
  assert.equal(await bridge.notify({ title: 'Done' }), false)
  assert.equal(notes.length, 0)
})

test('permission is requested once, on the first user gesture only', () => {
  const { fire, FakeNotification, ctx } = load({ permission: 'default' })
  assert.equal(FakeNotification.asked, 0, 'not at load')
  fire('pointerdown'); fire('keydown'); fire('pointerdown')
  assert.equal(FakeNotification.asked, 1)
  assert.equal(ctx.localStorage.getItem('hermes.web.notifyAsked'), '1')
})

test('never re-asks after an earlier answer', () => {
  const a = load({ permission: 'default', local: { 'hermes.web.notifyAsked': '1' } }); a.fire('pointerdown')
  assert.equal(a.FakeNotification.asked, 0)
  const b = load({ permission: 'denied' }); b.fire('pointerdown')
  assert.equal(b.FakeNotification.asked, 0)
})

test('granted: shows notification; click focuses tab and routes to the session', async () => {
  const { bridge, notes, window } = load({ permission: 'granted' })
  const focused = [], activated = []
  bridge.onFocusSession(id => focused.push(id))
  bridge.onNotificationActivate(p => activated.push(p))
  assert.equal(await bridge.notify({ title: 'Task finished', body: 'b', sessionId: 's1', activate: 'hermes://x', notifyId: 'n1' }), true)
  assert.equal(notes[0].title, 'Task finished')
  notes[0].onclick()
  assert.equal(window.focused, 1)
  assert.deepEqual(focused, ['s1'])
  assert.equal(activated[0].notifyId, 'n1')
  assert.equal(notes[0].closed, true)
})

test('notify tolerates junk payloads', async () => {
  const { bridge, notes } = load({ permission: 'granted' })
  for (const p of [undefined, null, {}, { title: 42 }]) assert.equal(await bridge.notify(p), true)
  assert.equal(notes[0].title, 'Hermes')
  notes[0].onclick() // no session → no throw
})

test('a throwing subscriber does not break the others', async () => {
  const { bridge, notes } = load({ permission: 'granted' })
  const got = []
  bridge.onFocusSession(() => { throw new Error('boom') })
  bridge.onFocusSession(id => got.push(id))
  await bridge.notify({ sessionId: 's9' })
  const orig = console.error; console.error = () => {}
  try { notes[0].onclick() } finally { console.error = orig }
  assert.deepEqual(got, ['s9'])
})

// ── Open in new window → new tab ─────────────────────────────────────────

test('openSessionWindow opens the same URL shape the desktop app builds', async () => {
  const { bridge, opened } = load()
  assert.equal((await bridge.openSessionWindow('abc 1/2', { profile: 'research', watch: true, connectionId: null })).ok, true)
  const u = new URL(opened[0].url, 'https://d.example')
  assert.equal(u.searchParams.get('win'), 'secondary')
  assert.equal(u.searchParams.get('watch'), '1')
  assert.equal(u.searchParams.get('profile'), 'research')
  assert.equal(u.searchParams.get('connectionId'), '')
  assert.equal(u.hash, '#/abc%201%2F2', 'session id is encoded into one route segment')
  assert.equal(opened[0].url.startsWith('/'), true, 'same-origin relative URL only')
})

test('openSessionWindow refuses bad ids and drops a bad profile', async () => {
  const { bridge, opened } = load()
  for (const id of ['', '  ', null, 7]) assert.equal((await bridge.openSessionWindow(id)).ok, false)
  assert.equal(opened.length, 0)
  await bridge.openSessionWindow('s', { profile: '../../x' })
  assert.equal(new URL(opened[0].url, 'https://d.example').searchParams.get('profile'), null)
})

test('injection attempts cannot change origin or add params', async () => {
  const { bridge, opened } = load()
  await bridge.openSessionWindow('//evil.example', {})
  await bridge.openSessionWindow('x?profile=evil&peer=1#/y', {})
  await bridge.openSessionWindow('javascript:alert(1)', {})
  for (const o of opened) {
    const u = new URL(o.url, 'https://d.example')
    assert.equal(u.origin, 'https://d.example')
    assert.deepEqual([...u.searchParams.keys()], ['win'])
  }
})

test('openWindow opens a peer tab; popup blocked is reported, not thrown', async () => {
  const a = load()
  assert.equal((await a.bridge.openWindow({ profile: 'ops', connectionId: null, profileWindow: true })).ok, true)
  const u = new URL(a.opened[0].url, 'https://d.example')
  assert.equal(u.searchParams.get('peer'), '1')
  assert.equal(u.searchParams.get('profile'), 'ops')
  assert.equal(u.searchParams.get('profileWindow'), '1')
  assert.equal((await a.bridge.openWindow()).ok, true)
  const b = load({ openReturns: null })
  assert.deepEqual({ ...(await b.bridge.openWindow()) }, { ok: false, error: 'popup-blocked' })
})

// ── Paste image button ───────────────────────────────────────────────────

const item = (type, size = 10) => ({ types: [type], getType: async () => new Blob([new Uint8Array(size)], { type }) })

test('paste image returns an attachable path whose bytes can be read back', async () => {
  const { bridge } = load({ clipboard: { read: async () => [item('text/plain'), item('image/png', 20)] } })
  const path = await bridge.saveClipboardImage()
  assert.match(path, /^\/browser-upload\/\d+\/pasted-image\.png$/)
  assert.match(await bridge.readFileDataUrlForAttach(path), /^data:image\/png;base64,/)
})

test('paste image returns "" for: no image, denied, unsupported, empty, oversize, svg', async () => {
  const cases = [
    { read: async () => [item('text/plain')] },
    { read: async () => { throw new Error('NotAllowedError') } },
    {},
    undefined,
    { read: async () => [item('image/png', 0)] },
    { read: async () => [{ types: ['image/png'], getType: async () => ({ size: 26 * 1024 * 1024 }) }] },
    { read: async () => [item('image/svg+xml')] }
  ]
  for (const clipboard of cases) assert.equal(await load({ clipboard }).bridge.saveClipboardImage(), '')
})

test('unanswered clipboard prompt gives up with "" instead of hanging', async () => {
  const fast = (f, ms) => setTimeout(f, ms >= 20000 ? 5 : ms)
  const { bridge } = load({ timers: fast, clipboard: { read: () => new Promise(() => {}) } })
  assert.equal(await bridge.saveClipboardImage(), '')
})

test('repeated paste clicks share one clipboard read (no prompt pile-up)', async () => {
  let reads = 0
  const { bridge } = load({ clipboard: { read: async () => { reads++; await new Promise(r => setTimeout(r, 10)); return [item('image/png', 5)] } } })
  const got = await Promise.all([1, 2, 3, 4, 5].map(() => bridge.saveClipboardImage()))
  assert.equal(reads, 1)
  assert.equal(new Set(got).size, 1)
  assert.match(got[0], /pasted-image\.png$/)
  await bridge.saveClipboardImage()
  assert.equal(reads, 2, 'a later click reads again')
})
