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
const WAYPOINT = readFileSync(new URL('waypoint/plugin.js', DIR))
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
  '/desktop-plugins/waypoint/plugin.js': WAYPOINT,
  ...extra
})

// ── The committed bundle itself ──────────────────────────────────────────

test('manifest lists exactly the reviewed plugins, in order', () => {
  assert.deepEqual(MANIFEST.plugins.map(p => p.name), ['profile-picker', 'waypoint', 'gmail'])
})

test('manifest pins waypoint to the reviewed upstream file and commit', () => {
  const p = MANIFEST.plugins.find(x => x.name === 'waypoint')
  assert.equal(p.source, 'https://github.com/mochiNJ/hermes-waypoint')
  assert.equal(p.commit, '971bab6762466bfdf6036aeaeb35352d6308f61d')
  assert.equal(p.version, '2.5.0')
  assert.equal(p.sha256, 'a0ac3637225cc324c6f08c45ce005edb87eb4f654a148cb22e92117ecafa3f15')
  assert.equal(sha(WAYPOINT), p.sha256)
})

test('manifest pins profile-picker to the reviewed upstream file and commit', () => {
  const p = MANIFEST.plugins[0]
  assert.equal(p.name, 'profile-picker')
  assert.equal(p.commit, '9a38fa52616c8b1be5397c4d922e80a2c754613d')
  assert.equal(p.sha256, '7afa6d249acfb0950516b82ce1758fb8f014149d18bf2283e29a57c362347047')
  assert.equal(sha(PICKER), p.sha256)
})

test('SHA256SUMS (checked at image build) agrees with the manifest; licence ships with it', () => {
  const sums = readFileSync(new URL('SHA256SUMS', DIR), 'utf8').trim().split('\n')
  assert.deepEqual(sums, MANIFEST.plugins.map(p => `${p.sha256}  ${p.name}/plugin.js`))
  for (const p of MANIFEST.plugins) {
    if (p.vendored === false) {
      // Not redistributed here: a NOTICE explains why, and no plugin code is committed.
      assert.ok(existsSync(new URL(`${p.name}/NOTICE`, DIR)), `${p.name} NOTICE`)
      assert.ok(!existsSync(new URL(`${p.name}/plugin.js`, DIR)), `${p.name} must not be vendored`)
    } else {
      assert.ok(existsSync(new URL(`${p.name}/LICENSE`, DIR)), p.name)
    }
  }
})

// gmail (upstream licence: Private) is fetched at image build from the pinned
// upstream commit; Docker's ADD --checksum fails the build on any byte change.
const GMAIL_SHA = '062af70aa6ce79baa3b4a231c5f934778b4682b6f1c4e3ceaecda6ee4cd0bf95'
const GMAIL_COMMIT = 'c8cd0c2d140561a1dffb7432c6d02b63b2374888'
const GMAIL_RAW = `https://raw.githubusercontent.com/guberm/hermes-gmail-desktop-plugin/${GMAIL_COMMIT}/plugins/gmail/desktop/plugin.js`

test('manifest and Dockerfile pin gmail to the reviewed upstream file, commit and checksum', () => {
  const p = MANIFEST.plugins.find(x => x.name === 'gmail')
  assert.equal(p.source, 'https://github.com/guberm/hermes-gmail-desktop-plugin')
  assert.equal(p.commit, GMAIL_COMMIT)
  assert.equal(p.version, '1.0.20')
  assert.equal(p.sha256, GMAIL_SHA)
  assert.equal(p.vendored, false)
  assert.equal(p.fetch, GMAIL_RAW)
  const docker = readFileSync(new URL('../deploy/robweb/Dockerfile', import.meta.url), 'utf8')
  const adds = docker.split('\n').filter(l => /^ADD\b/.test(l))
  assert.deepEqual(adds, [`ADD --checksum=sha256:${GMAIL_SHA} --chmod=644 ${GMAIL_RAW} /usr/share/nginx/html/desktop-plugins/gmail/plugin.js`])
})

// Reviewed exception, checked when a local copy is supplied (HDW_GMAIL_PLUGIN_JS=<path>):
// no network/storage/eval/import; the only raw HTML use is parsing the backend-sanitised
// email body into a detached inert <template>, which React then rebuilds from an allowlist.
test('gmail plugin (local copy, optional) matches the pin and the reviewed browser surface', { skip: !process.env.HDW_GMAIL_PLUGIN_JS }, () => {
  const buf = readFileSync(process.env.HDW_GMAIL_PLUGIN_JS)
  assert.equal(sha(buf), GMAIL_SHA)
  const src = buf.toString('utf8')
  for (const bad of [/\bfetch\s*\(/, /XMLHttpRequest/, /WebSocket/, /\beval\s*\(/, /new\s+Function\s*\(/,
    /localStorage|sessionStorage|indexedDB/, /import\s*\(/, /outerHTML|insertAdjacentHTML|dangerouslySetInnerHTML/,
    /postMessage|BroadcastChannel|sendBeacon|EventSource/, /window\.open/]) {
    assert.doesNotMatch(src, bad, String(bad))
  }
  assert.deepEqual(src.match(/[\w.]+\.innerHTML\s*=/g), ['fragment.innerHTML ='])
  assert.deepEqual(src.match(/document\.createElement\('[a-z]+'\)/g), ["document.createElement('template')"])
  assert.deepEqual([...new Set(src.match(/host\.request\('([a-z.]+)'/g) || [])], [])
})

// Waypoint reviewed exception: read-only layout measurement to place its card
// beside the sidebar (document.querySelector / getBoundingClientRect /
// window.innerWidth|innerHeight) and one user-clicked clipboard export. Pinned by
// count so any new browser access in an update fails here and gets re-reviewed.
test('waypoint makes no network, raw storage, eval or import calls; DOM use is the reviewed set', () => {
  const src = WAYPOINT.toString('utf8')
  for (const bad of [/\bfetch\s*\(/, /XMLHttpRequest/, /WebSocket/, /\beval\s*\(/, /new\s+Function\s*\(/,
    /localStorage|sessionStorage|indexedDB/, /import\s*\(/, /innerHTML|outerHTML|insertAdjacentHTML|dangerouslySetInnerHTML/,
    /postMessage|BroadcastChannel|sendBeacon|EventSource/]) {
    assert.doesNotMatch(src, bad, String(bad))
  }
  const uses = re => (src.match(re) || []).length
  assert.equal(uses(/\bdocument\.[A-Za-z.]+/g), 4)
  assert.deepEqual([...new Set(src.match(/\bdocument\.[A-Za-z.]+/g))].sort(), ['document.body', 'document.documentElement.dir', 'document.querySelector'])
  assert.deepEqual([...new Set(src.match(/\bwindow\.[A-Za-z.]+/g))].sort(), ['window.innerHeight', 'window.innerWidth'])
  assert.deepEqual(src.match(/\bnavigator\.[A-Za-z.]+/g), ['navigator.clipboard.writeText'])
  // Only these gateway calls; nothing that sends, deletes or changes config.
  assert.deepEqual([...new Set(src.match(/host\.request\('([a-z.]+)'/g))].sort(),
    ["host.request('llm.oneshot'", "host.request('session.archive'", "host.request('session.history'", "host.request('session.list'"])
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
  assert.deepEqual(JSON.parse(JSON.stringify(root.entries)), [
    { name: 'profile-picker', path: '/desktop-plugins/profile-picker', isDirectory: true },
    { name: 'waypoint', path: '/desktop-plugins/waypoint', isDirectory: true },
    { name: 'gmail', path: '/desktop-plugins/gmail', isDirectory: true }
  ])
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
  const w = await d.readPluginSource('/desktop-plugins/waypoint/plugin.js')
  assert.equal(w.text, WAYPOINT.toString('utf8'))
  assert.equal(w.truncated, false)
})

test('a tampered plugin file is refused, never handed to the loader', async () => {
  const { d } = load(served({ '/desktop-plugins/profile-picker/plugin.js': Buffer.concat([PICKER, Buffer.from('\n;alert(1)')]) }))
  await assert.rejects(d.readPluginSource('/desktop-plugins/profile-picker/plugin.js'), /checksum/)
  const r = await d.readFileText('/desktop-plugins/profile-picker/plugin.js')
  assert.equal(r.ok, false)
  assert.equal(r.text, undefined)
  const w = load(served({ '/desktop-plugins/waypoint/plugin.js': Buffer.concat([WAYPOINT, Buffer.from('\n;fetch("//x")')]) }))
  await assert.rejects(w.d.readPluginSource('/desktop-plugins/waypoint/plugin.js'), /checksum/)
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
