// Enumerate native preload API vs web-bridge API; classify bridge entries.
const fs = require('fs'), path = require('path'), Module = require('module')
const [preloadPath, bridgePath] = process.argv.slice(2)

function flatten(obj, prefix = '', out = {}) {
  for (const k of Object.keys(obj || {})) {
    const v = obj[k], p = prefix + k
    if (v && typeof v === 'object' && !Array.isArray(v)) flatten(v, p + '.', out)
    else out[p] = v
  }
  return out
}

// --- native: run compiled preload with a stub electron
let native = {}
const stubIpc = new Proxy({}, { get: () => () => ({}) })
const electron = {
  contextBridge: { exposeInMainWorld: (name, api) => { if (name === 'hermesDesktop') native = api } },
  ipcRenderer: stubIpc, webFrame: new Proxy({}, { get: () => () => 1 }),
  webUtils: { getPathForFile: () => '' },
}
const origLoad = Module._load
Module._load = function (req, ...rest) { return req === 'electron' ? electron : origLoad.call(this, req, ...rest) }
global.window = global; global.document = undefined
process.argv.push('--hermes-flags={}')
try { require(path.resolve(preloadPath)) } catch (e) { console.error('preload err', e.message) }
Module._load = origLoad

// --- web bridge: run in fake browser globals
const vm = require('vm')
const store = {}
const ctx = {
  location: { protocol: 'https:', origin: 'https://x', host: 'x', pathname: '/', search: '', href: 'https://x/' },
  sessionStorage: { getItem: k => store[k] ?? null, setItem: (k, v) => (store[k] = v), removeItem: k => delete store[k] },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  fetch: async () => ({ ok: true, status: 200, text: async () => '{}', json: async () => ({}), headers: { get: () => 'application/json' } }),
  crypto: require('crypto').webcrypto, console, URL, URLSearchParams, setTimeout, clearTimeout, setInterval, clearInterval,
  navigator: { userAgent: 'node', clipboard: {}, platform: 'MacIntel' }, matchMedia: () => ({ matches: false, addEventListener() {} }),
  addEventListener() {}, removeEventListener() {}, dispatchEvent() {}, Blob, FormData: class {}, WebSocket: class {},
  EventSource: class {}, Event: class {}, CustomEvent: class {}, BroadcastChannel: class { postMessage() {} close() {} },
}
ctx.window = ctx; ctx.globalThis = ctx; ctx.self = ctx
vm.createContext(ctx)
vm.runInContext(fs.readFileSync(bridgePath, 'utf8'), ctx)
const web = ctx.hermesDesktop || ctx.window.hermesDesktop || {}

const N = flatten(native), W = flatten(web)
const src = fs.readFileSync(bridgePath, 'utf8')
const stubRe = /^(\(\)\s*=>\s*(\{\s*\}|undefined|null|false|true|\[\]|\(\) => \{\})|async \(\)\s*=>\s*(undefined|null|false|\[\]|\{\}|\(\{[^}]*\}\))|noop|asyncNoop|subscribe)$/
const rows = []
for (const k of Object.keys(N).sort()) {
  if (!(k in W)) { rows.push([k, 'MISSING']); continue }
  const v = W[k]
  if (typeof v !== 'function') { rows.push([k, 'value:' + JSON.stringify(v)]); continue }
  const s = v.toString().replace(/\s+/g, ' ').trim()
  const named = ['noop', 'asyncNoop', 'subscribe'].find(n => v === vm.runInContext('typeof ' + n, ctx) && false)
  rows.push([k, stubRe.test(s) || s.length < 45 ? 'STUB? ' + s.slice(0, 70) : 'impl'])
}
const extra = Object.keys(W).filter(k => !(k in N))
console.log(JSON.stringify({ nativeCount: Object.keys(N).length, webCount: Object.keys(W).length, rows, extra }, null, 1))
