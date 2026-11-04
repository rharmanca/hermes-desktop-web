// Server-approved Desktop plugins (web-bridge.js + deploy/robweb/desktop-plugins).
// Run with: node --test tests/*.test.mjs
// The bridge must hand the app's plugin loader ONLY files listed in the
// manifest whose bytes match the pinned SHA-256, and never install/remove.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { readFileSync, existsSync } from 'node:fs'
import vm from 'node:vm'

const SRC = readFileSync(new URL('../web-bridge.js', import.meta.url), 'utf8')
const DIR = new URL('../deploy/robweb/desktop-plugins/', import.meta.url)
const MANIFEST = JSON.parse(readFileSync(new URL('manifest.json', DIR), 'utf8'))
const PICKER = readFileSync(new URL('profile-picker/plugin.js', DIR))
const plain = v => JSON.parse(JSON.stringify(v)) // sandbox arrays are another realm
const sha = buf => createHash('sha256').update(buf).digest('hex')

function storage() {
  const m = new Map()
  return { getItem: k => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: k => m.delete(k) }
}

// files: { '/desktop-plugins/x': Buffer|string|object } served with 200; else 404.
function load(files, { failManifest = false } = {}) {
  const calls = []
  const fetch = async url => {
    calls.push(String(url))
    if (failManifest && String(url).endsWith('/manifest.json')) throw new TypeError('network down')
    const body = files[String(url)]
    if (body === undefined) return { ok: false, status: 404, json: async () => ({}), arrayBuffer: async () => new ArrayBuffer(0) }
    const buf = Buffer.isBuffer(body) ? body : Buffer.from(typeof body === 'string' ? body : JSON.stringify(body))
    return {
      ok: true, status: 200,
      json: async () => JSON.parse(buf.toString('utf8')),
      arrayBuffer: async () => buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength)
    }
  }
  const location = { protocol: 'https:', host: 'd.example', origin: 'https://d.example', pathname: '/', search: '', reload() {}, replace() {} }
  const window = {}
  const ctx = {
    window, location, fetch, sessionStorage: storage(), localStorage: storage(), URL, Blob, FormData,
    TextDecoder, setTimeout, clearTimeout, console, Promise, navigator: { userAgent: 'test' }, crypto: globalThis.crypto
  }
  vm.createContext(ctx)
  vm.runInContext(SRC, ctx, { filename: 'web-bridge.js' })
  return { d: window.hermesDesktop, calls }
}

const served = (extra = {}) => ({
  '/desktop-plugins/manifest.json': MANIFEST,
  '/desktop-plugins/profile-picker/plugin.js': PICKER,
  ...extra
})

// ── The committed bundle itself ──────────────────────────────────────────

test('manifest pins profile-picker to the reviewed upstream file and commit', () => {
  const [p, ...rest] = MANIFEST.plugins
  assert.equal(rest.length, 0)
  assert.equal(p.name, 'profile-picker')
  assert.equal(p.commit, '9a38fa52616c8b1be5397c4d922e80a2c754613d')
  assert.equal(p.sha256, '7afa6d249acfb0950516b82ce1758fb8f014149d18bf2283e29a57c362347047')
  assert.equal(sha(PICKER), p.sha256)
})

test('SHA256SUMS (checked at image build) agrees with the manifest; licence ships with it', () => {
  const sums = readFileSync(new URL('SHA256SUMS', DIR), 'utf8').trim().split('\n')
  assert.deepEqual(sums, MANIFEST.plugins.map(p => `${p.sha256}  ${p.name}/plugin.js`))
  assert.ok(existsSync(new URL('profile-picker/LICENSE', DIR)))
})

test('bundled plugin makes no network, storage, DOM, or eval calls', () => {
  const src = PICKER.toString('utf8')
  for (const bad of [/\bfetch\s*\(/, /XMLHttpRequest/, /WebSocket/, /\beval\s*\(/, /new\s+Function\s*\(/,
    /localStorage|sessionStorage|indexedDB/, /\bdocument\./, /\bwindow\./, /import\s*\(/, /navigator\./]) {
    assert.doesNotMatch(src, bad, String(bad))
  }
})

// ── Bridge behaviour ─────────────────────────────────────────────────────

test('root, listing and entry follow the manifest only', async () => {
  const { d } = load(served())
  assert.equal(await d.desktopPluginsRoot(), '/desktop-plugins')
  assert.deepEqual(plain(await d.reconcileDesktopPlugins()), [])
  const root = await d.readDir('/desktop-plugins')
  assert.deepEqual(JSON.parse(JSON.stringify(root.entries)), [{ name: 'profile-picker', path: '/desktop-plugins/profile-picker', isDirectory: true }])
  const folder = await d.readDir('/desktop-plugins/profile-picker')
  assert.deepEqual(JSON.parse(JSON.stringify(folder.entries)), [{ name: 'plugin.js', path: '/desktop-plugins/profile-picker/plugin.js', isDirectory: false }])
  await assert.rejects(d.readDir('/desktop-plugins/other'))
})

test('verified plugin source is returned in full', async () => {
  const { d } = load(served())
  const r = await d.readPluginSource('/desktop-plugins/profile-picker/plugin.js')
  assert.equal(r.text, PICKER.toString('utf8'))
  assert.equal(r.truncated, false)
  const viaPreview = await d.readFileText('/desktop-plugins/profile-picker/plugin.js')
  assert.equal(viaPreview.text, PICKER.toString('utf8'))
})

test('a tampered plugin file is refused, never handed to the loader', async () => {
  const { d } = load(served({ '/desktop-plugins/profile-picker/plugin.js': Buffer.concat([PICKER, Buffer.from('\n;alert(1)')]) }))
  await assert.rejects(d.readPluginSource('/desktop-plugins/profile-picker/plugin.js'), /checksum/)
  const r = await d.readFileText('/desktop-plugins/profile-picker/plugin.js')
  assert.equal(r.ok, false)
  assert.equal(r.text, undefined)
})

test('bad manifest entries are dropped: traversal, odd names, bad hashes, duplicates', async () => {
  const good = MANIFEST.plugins[0]
  const { d } = load(served({
    '/desktop-plugins/manifest.json': { plugins: [
      good, { ...good, sha256: '0'.repeat(64) },
      { name: '../etc', sha256: good.sha256 }, { name: 'a/b', sha256: good.sha256 }, { name: 'UP', sha256: good.sha256 },
      { name: 'x', sha256: 'nothex' }, { name: 'y' }, null, 'z'
    ] }
  }))
  const root = await d.readDir('/desktop-plugins')
  assert.deepEqual(plain(root.entries.map(e => e.name)), ['profile-picker'])
  // First entry wins: the duplicate's zero hash must not replace the real one.
  assert.ok((await d.readPluginSource('/desktop-plugins/profile-picker/plugin.js')).text)
})

test('unlisted or crafted paths are refused without fetching them', async () => {
  const { d, calls } = load(served())
  for (const p of ['/desktop-plugins/evil/plugin.js', '/desktop-plugins/../web-bridge.js',
    '/desktop-plugins/profile-picker/../../index.html', '/desktop-plugins/profile-picker/plugin.js?x', '/desktop-plugins/manifest.json']) {
    await assert.rejects(d.readPluginSource(p), /Not a server-approved/, p)
  }
  assert.deepEqual(calls.filter(u => !u.endsWith('/manifest.json')), [])
})

test('browser cannot install or remove plugins', async () => {
  const { d, calls } = load(served())
  const before = calls.length
  assert.equal((await d.installDesktopPlugin({ identifier: 'https://github.com/x/y' })).ok, false)
  assert.equal((await d.removeDesktopPlugin({ name: 'profile-picker' })).ok, false)
  assert.equal(calls.length, before)
})

test('root watch is a fixed id (no polling); other paths keep the old no-op', async () => {
  const { d } = load(served())
  const w = await d.watchDirectory('/desktop-plugins')
  assert.equal(w.id, 'hdw-desktop-plugins-root')
  assert.equal(await d.watchDirectory('/home'), null)
  assert.equal(await d.readDir('/home'), null)
  assert.equal(await d.readFileText('/etc/passwd'), null)
})

test('manifest outage loads nothing and is retried on the next scan', async () => {
  const files = served()
  const first = load(files, { failManifest: true })
  assert.deepEqual(plain((await first.d.readDir('/desktop-plugins')).entries), [])
  assert.deepEqual(plain((await first.d.readDir('/desktop-plugins')).entries), [])
  assert.equal(first.calls.filter(u => u.endsWith('/manifest.json')).length, 2)
})

test('missing manifest (404) means no plugins', async () => {
  const { d } = load({})
  assert.deepEqual(plain((await d.readDir('/desktop-plugins')).entries), [])
  await assert.rejects(d.readPluginSource('/desktop-plugins/profile-picker/plugin.js'))
})
