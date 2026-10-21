// web-bridge.js — replaces Electron's preload bridge (window.hermesDesktop)
// with a browser-native implementation talking to the SAME-ORIGIN gateway.
// The desktop renderer only ever sees this object; it cannot tell the
// difference. Auth rides the gateway's own dashboard cookie session:
// unauthenticated REST gets a 401/302 which we bounce to /login, and WS
// upgrades use single-use tickets minted via POST /api/auth/ws-ticket
// (the exact flow the dashboard web app uses).
(() => {
  'use strict'

  const wsScheme = location.protocol === 'https:' ? 'wss' : 'ws'
  const noop = () => {}
  const asyncNoop = async () => undefined
  const subscribe = () => () => {}

  let redirecting = false

  // An expired edge sign-in (e.g. Cloudflare Access) answers with a redirect
  // to another origin: fetch() then fails as a bare TypeError, or follows to
  // an HTML page. The app swallows mint errors and dials an empty URL, which
  // surfaces as a meaningless "connect() requires a ws:// URL" toast. Detect
  // that case and reload once so the edge can show its sign-in page.
  const REAUTH_KEY = 'hermes.web.reauthAt'
  async function edgeSignInExpired() {
    try {
      const probe = await fetch('/api/status', { credentials: 'same-origin', redirect: 'manual', cache: 'no-store' })
      return probe.type === 'opaqueredirect'
    } catch { return true }
  }
  // Sign-in is gone but we already reloaded in the last minute: show a plain
  // message with a button instead of letting the app dial an empty URL
  // ("connect() requires a ws:// URL"). The pending ticket request never
  // settles, so the app sits quietly behind the banner until the user acts.
  function showSignInBanner() {
    if (typeof document === 'undefined' || document.getElementById('hermes-web-signin')) return
    const box = document.createElement('div')
    box.id = 'hermes-web-signin'
    box.setAttribute('role', 'alert')
    box.style.cssText = 'position:fixed;top:12px;left:50%;transform:translateX(-50%);z-index:2147483647;' +
      'background:#1f2937;color:#fff;padding:10px 14px;border-radius:8px;font:14px system-ui,sans-serif;' +
      'box-shadow:0 4px 16px rgba(0,0,0,.3);display:flex;gap:12px;align-items:center'
    box.textContent = 'Your sign-in has expired.'
    const btn = document.createElement('button')
    btn.textContent = 'Sign in again'
    btn.style.cssText = 'background:#fff;color:#111;border:0;border-radius:6px;padding:4px 10px;cursor:pointer'
    btn.onclick = () => { sessionStorage.removeItem(REAUTH_KEY); location.reload() }
    box.appendChild(btn)
    ;(document.body || document.documentElement).appendChild(box)
  }
  const SIGNED_OUT = () => { showSignInBanner(); return new Promise(() => {}) }

  function reauthAtEdge() {
    const last = Number(sessionStorage.getItem(REAUTH_KEY) || 0)
    if (redirecting || Date.now() - last < 60000) return false
    redirecting = true
    sessionStorage.setItem(REAUTH_KEY, String(Date.now()))
    location.reload()
    return true
  }

  async function mintTicket() {
    let res
    try {
      res = await fetch('/api/auth/ws-ticket', { method: 'POST', credentials: 'same-origin' })
    } catch (err) {
      if (await edgeSignInExpired()) {
        if (reauthAtEdge()) throw new Error('sign-in expired: reloading to sign in again')
        return SIGNED_OUT()
      }
      throw err
    }
    if (res.status === 401) { toLogin(); throw new Error('401: login required') }
    if (!res.ok) throw new Error(`${res.status}: ws-ticket mint failed`)
    const isJson = (res.headers.get('content-type') || '').includes('application/json')
    if (res.redirected || !isJson) {
      if (reauthAtEdge()) throw new Error('sign-in expired: reloading to sign in again')
      return SIGNED_OUT()
    }
    return (await res.json()).ticket
  }

  function toLogin() {
    if (redirecting) return
    redirecting = true
    location.replace(`/login?next=${encodeURIComponent(location.pathname + location.search)}`)
  }

  // ── REST: the one data path. Mirrors electron/main.ts fetchJsonForBackend
  //    (JSON in/out, `404: body`-style rejections, multipart uploads).
  async function api(request = {}) {
    let path = request.path

    if (request.profile) {
      const u = new URL(path, location.origin)

      if (!u.searchParams.has('profile')) u.searchParams.set('profile', request.profile)
      path = u.pathname + u.search
    }

    const init = { method: request.method || 'GET', credentials: 'same-origin' }

    if (request.upload) {
      const form = new FormData()

      form.append('file', new Blob([request.upload.bytes], { type: request.upload.contentType || 'application/octet-stream' }), request.upload.filename || 'file')
      init.body = form
    } else if (request.body !== undefined) {
      init.headers = { 'Content-Type': 'application/json' }
      init.body = JSON.stringify(request.body)
    }

    const res = await fetch(path, init)
    const text = await res.text()

    if (res.status === 401 && !redirecting) { toLogin(); throw new Error('401: login required') }
    if (res.status >= 400) {
      const err = new Error(`${res.status}: ${text}`)

      err.statusCode = res.status
      throw err
    }
    if (!text) return null

    return JSON.parse(text)
  }

  // ── WS: fresh single-use ticket per dial (30s TTL, consumed on upgrade).
  async function gatewayWsUrl() {
    const ticket = await mintTicket()
    const enc = encodeURIComponent

    return `${wsScheme}://${location.host}/api/ws?ticket=${enc(ticket)}`
  }

  // ── Connection descriptor: what getConnection() resolves. One backend,
  //    this origin, cookie auth — the registry/multi-connection world does
  //    not exist in web mode.
  const connection = {
    baseUrl: location.origin,
    customWindowControls: false,
    isFullscreen: false,
    mode: 'remote',
    authMode: 'token',
    remoteKind: 'url',
    nativeOverlayWidth: 0,
    source: 'env',
    token: '',
    wsUrl: '',
    logs: [],
    windowButtonPosition: null
  }

  // ── Client-side attachments. Electron's contract: selectPaths() returns
  // paths on THIS machine, and readFileDataUrl(path) hands back their bytes
  // (remote mode always ships attachments as bytes; the path is only ever a
  // handle to re-read them). In web mode the browser owns the bytes, so
  // paths are tokens into this map.
  const clientFiles = new Map()
  let attachSeq = 0

  function blobToDataUrl(blob) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader()

      reader.onload = () => resolve(reader.result)
      reader.onerror = () => reject(reader.error)
      reader.readAsDataURL(blob)
    })
  }

  // Bytes for a token path. null on miss → the app falls back to the
  // gateway-side /api/fs read for gateway-owned paths. The app reaches this
  // under two names (readFileDataUrlForAttach ?? readFileDataUrl) — BOTH must
  // exist, or the universal Proxy shadows the ?? fallback and every attach
  // read resolves to null ("Could not read <file>").
  async function readClientFileDataUrl(path) {
    const entry = clientFiles.get(path)

    if (!entry) return null

    return blobToDataUrl(entry instanceof Blob ? entry : new Blob([entry]))
  }

  // ── Terminal tab: gateway-side shell PTY via the web-terminal plugin
  // (same role as the desktop app's SSH-terminal path: the shell lives where
  // Hermes lives). One WebSocket per terminal id; resize rides the dashboard
  // ESC[RESIZE:cols;rows] escape on the input stream.
  const terminals = new Map()

  const terminalApi = {
    async start(options = {}) {
      const info = await fetch('/api/plugins/web-terminal/shell', { credentials: 'same-origin' })
        .then(r => (r.ok ? r.json() : {})).catch(() => ({}))
      const params = new URLSearchParams()

      if (options.cols) params.set('cols', String(options.cols))
      if (options.rows) params.set('rows', String(options.rows))
      if (options.cwd) params.set('cwd', String(options.cwd))
      const dialUrl = ticket =>
        `${wsScheme}://${location.host}/api/plugins/web-terminal/terminal?${params}&ticket=${encodeURIComponent(ticket)}`

      // The gateway's WS tickets are single-use with a 30 s TTL. If the browser
      // queues the upgrade past that (busy page load, connection-slot wait), the
      // gateway rejects it as expired (close 1008) and the tab reads as dead.
      // One fresh-ticket retry covers that window; anything else surfaces with
      // its close code instead of a blanket "unreachable".
      const dial = () => new Promise((resolve, reject) => {
        void mintTicket().then(ticket => {
          const ws = new WebSocket(dialUrl(ticket))

          ws.binaryType = 'arraybuffer'
          const fail = code => {
            try { ws.close() } catch { /* never opened */ }
            reject(Object.assign(
              new Error(`gateway terminal unreachable (close ${code ?? 'error'})`), { code }))
          }
          ws.onopen = () => resolve(ws)
          ws.onerror = () => { /* the close event carries the code; it decides */ }
          ws.onclose = e => ws.readyState === 3 && fail(e.code)
          setTimeout(() => ws.readyState !== 1 && fail('timeout'), 8000)
        }).catch(reject)
      })

      let ws
      try {
        ws = await dial()
      } catch (e) {
        if (e.code === 1008 || e.code === 'timeout') ws = await dial()
        else throw e
      }

      const id = crypto.randomUUID()
      const term = { data: new Set(), exit: new Set(), exited: false, ws }

      terminals.set(id, term)
      ws.onmessage = e => {
        const text = typeof e.data === 'string' ? e.data : new TextDecoder().decode(e.data)

        term.data.forEach(cb => cb(text))
      }
      ws.onclose = () => {
        if (!term.exited) {
          term.exited = true
          term.exit.forEach(cb => cb({ code: null, signal: null }))
        }
        terminals.delete(id)
      }

      return { cwd: info.cwd || '', id, shell: info.shell || 'shell' }
    },
    async attach(id) {
      const t = terminals.get(id)

      return !!t && t.ws.readyState === 1
    },
    async write(id, data) {
      const t = terminals.get(id)

      if (!t || t.ws.readyState !== 1) return false
      t.ws.send(data)

      return true
    },
    async resize(id, size) {
      const t = terminals.get(id)

      if (!t || t.ws.readyState !== 1) return false
      t.ws.send(`\x1b[RESIZE:${size.cols};${size.rows}]`)

      return true
    },
    async cwd() { return null },
    async dispose(id) {
      const t = terminals.get(id)

      if (t) {
        try { t.ws.close() } catch { /* already gone */ }
        terminals.delete(id)
      }

      return true
    },
    onData(id, cb) {
      const t = terminals.get(id)

      if (t) t.data.add(cb)

      return () => t && t.data.delete(cb)
    },
    onExit(id, cb) {
      const t = terminals.get(id)

      if (t) t.exit.add(cb)

      return () => t && t.exit.delete(cb)
    }
  }

  const bridge = {
    api,
    terminal: terminalApi,
    // Native browser file picker; resolves with token paths ([] on cancel).
    selectPaths: (options = {}) => new Promise(resolve => {
      const input = document.createElement('input')

      input.type = 'file'
      input.multiple = options.multiple !== false
      input.style.display = 'none'
      if (Array.isArray(options.filters)) {
        const exts = options.filters.flatMap(f => f.extensions || [])

        if (exts.length) input.accept = exts.map(e => `.${String(e).replace(/^\./, '')}`).join(',')
      }
      document.body.appendChild(input)

      const done = paths => { input.remove(); resolve(paths) }

      input.addEventListener('change', () => done([...input.files].map(file => {
        // Token keeps the real filename as the basename (the app derives
        // upload filenames from it); the counter dir keeps names unique.
        const path = `/browser-upload/${++attachSeq}/${file.name}`

        clientFiles.set(path, file)

        return path
      })))
      input.addEventListener('cancel', () => done([]))
      input.click()
    }),
    readFileDataUrl: readClientFileDataUrl,
    readFileDataUrlForAttach: readClientFileDataUrl,
    // Drop/paste image path: the app hands us bytes, we hand back a token.
    saveImageBuffer: async (data, ext, name) => {
      const path = `/browser-upload/${++attachSeq}/${name || `image.${ext}`}`

      clientFiles.set(path, data instanceof Uint8Array ? data : new Uint8Array(data))

      return path
    },
    getConnection: async () => connection,
    getConnectionFor: async () => connection,
    revalidateConnection: async () => ({ ok: true, rebuilt: false }),
    getGatewayWsUrl: gatewayWsUrl,
    getGatewayWsUrlFor: gatewayWsUrl,
    getProfileRoutes: async () => [],
    getAgentRoster: async () => ({ agents: [] }),
    getVersion: async () => {
      const health = await fetch('/api/health', { credentials: 'same-origin' }).then(r => r.json()).catch(() => ({}))

      return {
        appVersion: health.version || 'web',
        channel: null,
        electronVersion: 'web',
        nodeVersion: navigator.userAgent,
        platform: 'web',
        hermesRoot: ''
      }
    },

    // Connection registry: exactly one entry — this origin.
    connections: {
      list: async () => ({ connections: [] }),
      save: asyncNoop,
      remove: asyncNoop,
      setPrimary: asyncNoop,
      setLaunchMode: asyncNoop,
      setLastUsed: asyncNoop,
      test: async () => ({ ok: true }),
      updateManaged: asyncNoop,
      updateAll: asyncNoop,
      onChanged: subscribe
    },

    // Browser equivalents where they exist, honest no-ops where they don't.
    windowControls: { custom: false, minimize: noop, maximize: noop, toggleMaximize: noop, close: noop },
    glassSupported: async () => false,
    translucencySupported: async () => false,
    setTranslucency: noop,
    setNativeTheme: noop,
    setTitleBarTheme: noop,
    openExternal: url => { window.open(url, '_blank', 'noopener') },
    writeClipboard: text => navigator.clipboard.writeText(text),
    readClipboard: () => navigator.clipboard.readText(),
    getPathForFile: file => {
      if (!(file instanceof Blob)) return ''
      const path = `/browser-upload/${++attachSeq}/${file.name || 'attachment'}`

      clientFiles.set(path, file)

      return path
    },
    notify: payload => {
      try { new Notification(payload.title || 'Hermes', { body: payload.body || '' }) } catch { /* no permission */ }
    },
    setActiveWork: noop,
    // Workspace cwd lives on the gateway host in web mode — echo requests
    // back unsanitized and report no configured default (the app's file
    // browser goes through the gateway's file API regardless).
    sanitizeWorkspaceCwd: async cwd => ({ cwd: cwd || '', sanitized: false }),
    settings: {
      getDefaultProjectDir: async () => ({ defaultLabel: 'Home', dir: null, resolvedCwd: '' }),
      setDefaultProjectDir: async dir => ({ dir: dir ?? null }),
      pickDefaultProjectDir: async () => ({ canceled: true, dir: null })
    },
    // The app's own non-native fallback for this one is localStorage —
    // mirror it so the universal proxy doesn't shadow the fallback.
    preparedSubmissions: {
      read: async () => localStorage.getItem('hermes.desktop.preparedSubmissions.v1') || '{}',
      update: async (key, entry) => {
        const all = JSON.parse(localStorage.getItem('hermes.desktop.preparedSubmissions.v1') || '{}')

        if (entry === null) delete all[key]; else all[key] = entry
        localStorage.setItem('hermes.desktop.preparedSubmissions.v1', JSON.stringify(all))
      },
      compareAndSet: async (key, expected, entry) => {
        const all = JSON.parse(localStorage.getItem('hermes.desktop.preparedSubmissions.v1') || '{}')

        if ((all[key] ?? null) !== expected) return { applied: false, current: all[key] ?? null }
        if (entry === null) delete all[key]; else all[key] = entry
        localStorage.setItem('hermes.desktop.preparedSubmissions.v1', JSON.stringify(all))

        return { applied: true, current: entry }
      }
    },
    // The browser owns page zoom (Ctrl +/-), so report a fixed 100%. Must be
    // explicit: the renderer destructures `{ percent }` from get() at startup,
    // and the universal fallback resolves null -> uncaught TypeError.
    zoom: {
      get: async () => ({ level: 0, percent: 100 }),
      factor: () => 1,
      setPercent: noop,
      onChanged: subscribe
    },
    claimStartupLatency: async () => false,
    getBootProgress: async () => null,
    requestMicrophoneAccess: async () => {
      try { (await navigator.mediaDevices.getUserMedia({ audio: true })).getTracks().forEach(t => t.stop()) } catch { return false }
      return true
    },

    // Main→renderer event pushes: nothing pushes in web mode.
    onBackendExit: subscribe,
    onBootProgress: subscribe,
    onBootstrapEvent: subscribe,
    onConnectionApplied: subscribe,
    onPowerResume: subscribe,
    onWindowStateChanged: subscribe,
    onDeepLink: subscribe,
    onFocusSession: subscribe,
    onNotificationAction: subscribe,
    onNotificationActivate: subscribe,
    onExternalOpenFailed: subscribe,
    onBrowserPopoutClosed: subscribe
  }

  // Long tail (~150 methods: windows, popouts, HUD, pet, native FS, updates):
  // any unknown property resolves to a UNIVERSAL object — callable (returns
  // undefined), and every property of it is itself universal. That covers
  // both `desktop.foo()` and namespaced `desktop.foo.onBar(cb)` shapes; the
  // renderer's guards treat these as an older/limited Electron build.
  // ONE shape satisfies every consumer the app has: callable (effects call
  // subscribe results as cleanup fns), thenable resolving to null (awaited
  // results; JSON.parse(null) is null), string-like (some call sites .trim()
  // the raw return), and infinitely property-addressable (namespaced
  // desktop.foo.onBar(cb)). A function-target Proxy covers all four.
  const stringish = {
    trim: () => '', trimStart: () => '', trimEnd: () => '', split: () => [],
    charAt: () => '', includes: () => false, startsWith: () => false, endsWith: () => false,
    replace: () => '', replaceAll: () => '', slice: () => '', substring: () => '',
    toLowerCase: () => '', toUpperCase: () => '', padStart: () => '', padEnd: () => '',
    match: () => null, matchAll: () => [][Symbol.iterator](), repeat: () => '', concat: () => '',
    indexOf: () => -1, lastIndexOf: () => -1, localeCompare: () => 0, normalize: () => ''
  }

  const universal = new Proxy(function () {}, {
    get(t, prop) {
      if (prop === 'then') return (res) => Promise.resolve(null).then(res)
      if (prop === 'catch') return () => Promise.resolve(null)
      if (prop === 'finally') return (fin) => Promise.resolve(null).finally(fin)
      if (prop === 'toString' || prop === 'valueOf') return () => ''
      if (prop === Symbol.toPrimitive) return () => ''
      if (prop === Symbol.iterator) return ''[Symbol.iterator].bind('')
      if (prop === Symbol.toStringTag) return undefined
      if (prop === 'length') return 0
      if (prop in stringish) return stringish[prop]

      return universal
    },
    apply() { return universal },
    set() { return true }
  })

  window.hermesDesktop = new Proxy(bridge, {
    get(target, prop) {
      if (prop in target) return target[prop]
      if (prop === 'then' || prop === Symbol.toPrimitive) return undefined

      return universal
    },
    set(target, prop, value) { target[prop] = value; return true }
  })
})()
