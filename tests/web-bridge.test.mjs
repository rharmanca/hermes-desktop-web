// Unit tests for web-bridge.js — run with: node --test tests/*.test.mjs
// Loads the real bridge file into an isolated sandbox with a fake browser
// (location, fetch, storage) so each behaviour is checked without a server.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

const SRC = readFileSync(new URL('../web-bridge.js', import.meta.url), 'utf8')

function makeStorage() {
  const m = new Map()
  return {
    getItem: k => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: k => m.delete(k),
    _map: m
  }
}

function res({ status = 200, body = '', type = 'basic', redirected = false, contentType = 'application/json' } = {}) {
  return {
    status, ok: status >= 200 && status < 300, type, redirected,
    headers: { get: h => (h.toLowerCase() === 'content-type' ? contentType : null) },
    text: async () => body,
    json: async () => JSON.parse(body)
  }
}

// routes: (url, init) => response | throw. Records every call.
function load({ routes, protocol = 'https:', session } = {}) {
  const calls = []
  const location = {
    protocol, host: 'desktop.example', origin: `${protocol}//desktop.example`,
    pathname: '/chat', search: '?x=1', reloads: 0, replacedWith: null,
    reload() { this.reloads++ },
    replace(u) { this.replacedWith = u }
  }
  const sessionStorage = session ?? makeStorage()
  const localStorage = makeStorage()
  const fetch = async (url, init = {}) => {
    calls.push({ url: String(url), init })
    return routes(String(url), init)
  }
  const window = {}
  const ctx = {
    window, location, fetch, sessionStorage, localStorage, URL, Blob, FormData,
    TextDecoder, setTimeout, clearTimeout, console, Promise,
    navigator: { userAgent: 'test' }, crypto: globalThis.crypto
  }
  vm.createContext(ctx)
  vm.runInContext(SRC, ctx, { filename: 'web-bridge.js' })
  return { bridge: window.hermesDesktop, location, calls, sessionStorage, localStorage }
}

const ticketOk = url =>
  url === '/api/auth/ws-ticket' ? res({ body: '{"ticket":"t/1+2","ttl_seconds":30}' }) : res({ status: 404 })

// ── WebSocket URL / ticket minting ───────────────────────────────────────

test('ws URL uses wss on https and URL-encodes the ticket', async () => {
  const { bridge } = load({ routes: ticketOk })
  assert.equal(await bridge.getGatewayWsUrl(), 'wss://desktop.example/api/ws?ticket=t%2F1%2B2')
})

test('ws URL uses ws on plain http', async () => {
  const { bridge } = load({ routes: ticketOk, protocol: 'http:' })
  assert.match(await bridge.getGatewayWsUrl(), /^ws:\/\/desktop\.example\/api\/ws\?ticket=/)
})

test('every dial mints a fresh ticket (tickets are single-use)', async () => {
  const { bridge, calls } = load({ routes: ticketOk })
  await bridge.getGatewayWsUrl(); await bridge.getGatewayWsUrlFor('hermes-admin')
  assert.equal(calls.filter(c => c.url === '/api/auth/ws-ticket').length, 2)
  assert.equal(calls[0].init.method, 'POST')
})

test('ticket 401 sends the user to /login with a return path', async () => {
  const { bridge, location } = load({ routes: () => res({ status: 401 }) })
  await assert.rejects(bridge.getGatewayWsUrl(), /401/)
  assert.equal(location.replacedWith, '/login?next=%2Fchat%3Fx%3D1')
})

test('ticket 500 rejects with the status and does not reload', async () => {
  const { bridge, location } = load({ routes: () => res({ status: 500 }) })
  await assert.rejects(bridge.getGatewayWsUrl(), /500: ws-ticket mint failed/)
  assert.equal(location.reloads, 0)
})

test('expired edge sign-in (network error + redirecting probe) reloads once', async () => {
  const { bridge, location, sessionStorage } = load({
    routes: url => {
      if (url === '/api/auth/ws-ticket') throw new TypeError('Failed to fetch')
      if (url === '/api/status') return res({ type: 'opaqueredirect', status: 0 })
    }
  })
  await assert.rejects(bridge.getGatewayWsUrl(), /sign-in expired/)
  assert.equal(location.reloads, 1)
  assert.ok(sessionStorage.getItem('hermes.web.reauthAt'))
})

test('expired edge sign-in: second failure within a minute does NOT reload again (no loop)', async () => {
  const session = makeStorage()
  const routes = url => {
    if (url === '/api/auth/ws-ticket') throw new TypeError('Failed to fetch')
    return res({ type: 'opaqueredirect', status: 0 })
  }
  load({ routes, session }).bridge.getGatewayWsUrl().catch(() => {})
  await new Promise(r => setTimeout(r, 10))
  const second = load({ routes, session }) // simulates the page after reload
  await assert.rejects(second.bridge.getGatewayWsUrl(), /Failed to fetch/)
  assert.equal(second.location.reloads, 0)
})

test('plain network outage (server unreachable, probe also fails) — reload guard still applies', async () => {
  const { bridge, location } = load({ routes: () => { throw new TypeError('Failed to fetch') } })
  await assert.rejects(bridge.getGatewayWsUrl())
  assert.ok(location.reloads <= 1)
})

test('network error but server answers the probe normally: no reload, original error surfaces', async () => {
  const { bridge, location } = load({
    routes: url => {
      if (url === '/api/auth/ws-ticket') throw new TypeError('Failed to fetch')
      return res({ body: '{}' })
    }
  })
  await assert.rejects(bridge.getGatewayWsUrl(), /Failed to fetch/)
  assert.equal(location.reloads, 0)
})

test('ticket answered with an HTML sign-in page (followed redirect) triggers re-sign-in', async () => {
  const { bridge, location } = load({
    routes: () => res({ body: '<html>login</html>', contentType: 'text/html', redirected: true })
  })
  await assert.rejects(bridge.getGatewayWsUrl(), /sign-in expired/)
  assert.equal(location.reloads, 1)
})

test('never resolves an empty ws URL (the original profile-switch bug)', async () => {
  for (const routes of [() => res({ status: 500 }), () => { throw new TypeError('x') },
    () => res({ body: '<html>', contentType: 'text/html' })]) {
    const { bridge } = load({ routes })
    const out = await bridge.getGatewayWsUrl().catch(() => 'rejected')
    assert.notEqual(out, '')
  }
})

// ── REST api() ───────────────────────────────────────────────────────────

test('api() adds the profile query param and keeps existing params', async () => {
  const { bridge, calls } = load({ routes: () => res({ body: '{"ok":true}' }) })
  assert.deepEqual({ ...(await bridge.api({ path: '/api/sessions?limit=5', profile: 'hermes-admin' })) }, { ok: true })
  assert.equal(calls[0].url, '/api/sessions?limit=5&profile=hermes-admin')
})

test('api() does not override an explicit profile param', async () => {
  const { bridge, calls } = load({ routes: () => res({ body: '{}' }) })
  await bridge.api({ path: '/api/x?profile=research', profile: 'hermes-admin' })
  assert.equal(calls[0].url, '/api/x?profile=research')
})

test('api() sends JSON bodies with method and same-origin credentials', async () => {
  const { bridge, calls } = load({ routes: () => res({ body: '' }) })
  assert.equal(await bridge.api({ path: '/api/y', method: 'POST', body: { a: 1 } }), null)
  assert.equal(calls[0].init.method, 'POST')
  assert.equal(calls[0].init.body, '{"a":1}')
  assert.equal(calls[0].init.headers['Content-Type'], 'application/json')
  assert.equal(calls[0].init.credentials, 'same-origin')
})

test('api() errors carry "status: body" and statusCode', async () => {
  const { bridge } = load({ routes: () => res({ status: 404, body: 'nope' }) })
  await assert.rejects(bridge.api({ path: '/api/z' }), e => e.message === '404: nope' && e.statusCode === 404)
})

test('api() 401 redirects to login exactly once', async () => {
  const { bridge, location } = load({ routes: () => res({ status: 401 }) })
  await assert.rejects(bridge.api({ path: '/api/a' }), /401/)
  location.replacedWith = 'unchanged'
  await assert.rejects(bridge.api({ path: '/api/b' }))
  assert.equal(location.replacedWith, 'unchanged')
})

// ── Contract shape the desktop renderer relies on ────────────────────────

test('connection descriptor is single-origin remote/token', async () => {
  const { bridge } = load({ routes: ticketOk })
  const c = await bridge.getConnection()
  assert.equal(c.baseUrl, 'https://desktop.example')
  assert.equal(c.mode, 'remote')
  assert.equal(c.authMode, 'token')
  assert.equal(await bridge.getConnectionFor('anything'), c)
})

test('void bridge methods return undefined, never a Promise (React cleanup crash)', () => {
  const { bridge } = load({ routes: ticketOk })
  for (const m of ['setNativeTheme', 'setTitleBarTheme', 'setTranslucency', 'setActiveWork']) {
    assert.equal(bridge[m]('x'), undefined, m)
  }
})

test('subscription methods return a callable unsubscribe', () => {
  const { bridge } = load({ routes: ticketOk })
  for (const m of ['onBackendExit', 'onDeepLink', 'onConnectionApplied', 'onPowerResume']) {
    assert.equal(typeof bridge[m](() => {}), 'function', m)
  }
})

test('unknown methods are safe no-ops (callable, awaitable to null, string-like)', async () => {
  const { bridge } = load({ routes: ticketOk })
  assert.equal(await bridge.someFutureMethod(), null)
  assert.equal(typeof bridge.pet.onSomething(() => {}), 'function')
  assert.equal(bridge.anything.trim(), '')
  assert.equal(`${bridge.whatever}`, '')
  assert.equal(bridge.then, undefined) // bridge itself must not look like a Promise
})

test('preparedSubmissions compareAndSet only applies when the expected value matches', async () => {
  const { bridge } = load({ routes: ticketOk })
  const ps = bridge.preparedSubmissions
  assert.equal((await ps.compareAndSet('k', null, 'v1')).applied, true)
  assert.deepEqual({ ...(await ps.compareAndSet('k', null, 'v2')) }, { applied: false, current: 'v1' })
  assert.equal((await ps.compareAndSet('k', 'v1', null)).applied, true)
  assert.equal(await ps.read(), '{}')
})

test('saveImageBuffer returns a readable token path', async () => {
  const { bridge } = load({ routes: ticketOk })
  const p = await bridge.saveImageBuffer(new Uint8Array([1, 2]), 'png')
  assert.match(p, /^\/browser-upload\/\d+\/image\.png$/)
})
