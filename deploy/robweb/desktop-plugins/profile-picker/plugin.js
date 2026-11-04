/**
 * Profile Picker — choose the profile right in the composer of a new chat.
 *
 * - New, unsent chat: a "Profile: <name>" dropdown sits at the start of the
 *   composer. Picking a profile opens a fresh draft in that profile
 *   (host.newChat(route)); text you already typed is carried over.
 * - Running chat: the same spot shows a read-only chip with the chat's
 *   profile (an existing session cannot be moved to another profile).
 * - With a single profile nothing is shown.
 * - Command palette: "New chat in profile: <name>" for every profile.
 *
 * Desktop-only. Uses the public Desktop plugin SDK exclusively; no network
 * calls, no file access, no storage, no patching of app internals.
 * Plain ESM, loaded uncompiled — only @hermes/plugin-sdk and react/jsx-runtime.
 */

import {
  COMPOSER_AREAS,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  host,
  PALETTE_AREA,
  profileColor,
  Tip,
  usePluginI18n,
  useQuery,
  useValue
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'profile-picker'
const VERSION = '1.1.0'

// ---------------------------------------------------------------------------
// Strings (English first; German bundled). Other locales fall back to English.
// ---------------------------------------------------------------------------

const MESSAGES = {
  en: {
    pickerLabel: name => `Profile: ${name}`,
    pickerAria: 'Choose the profile for this new chat',
    menuHeader: 'New chat in profile…',
    chipTip: 'Profile of this chat (cannot be changed)',
    paletteNew: name => `New chat in profile: ${name}`,
    openFailed: name => `Could not open profile "${name}"`,
    inactiveTitle: 'Profile Picker inactive',
    inactiveMessage: 'This Hermes Desktop version lacks the plugin APIs it needs. Please update the app.'
  },
  de: {
    pickerLabel: name => `Profil: ${name}`,
    pickerAria: 'Profil für die neue Session wählen',
    menuHeader: 'Neue Session in Profil …',
    chipTip: 'Profil dieser Session (nicht änderbar)',
    paletteNew: name => `Neue Session in Profil: ${name}`,
    openFailed: name => `Profil „${name}“ konnte nicht geöffnet werden`,
    inactiveTitle: 'Profil-Picker inaktiv',
    inactiveMessage: 'Diese Version der Desktop-App bietet die nötige Plugin-Schnittstelle nicht. Bitte die App aktualisieren.'
  }
}

/** Fallback translator (English) until ctx.i18n is wired up in register(). */
function fallbackTranslate(key, ...args) {
  const value = MESSAGES.en[key]
  return typeof value === 'function' ? value(...args) : (value ?? key)
}

let translate = fallbackTranslate

function useT() {
  // usePluginI18n re-renders on locale switches; fall back if unavailable.
  return typeof usePluginI18n === 'function' ? usePluginI18n(ID) : fallbackTranslate
}

// ---------------------------------------------------------------------------
// Capability check — on older apps register nothing and say so once.
// ---------------------------------------------------------------------------

function missingCapabilities() {
  const missing = []
  if (!COMPOSER_AREAS || !COMPOSER_AREAS.leading) missing.push('COMPOSER_AREAS.leading')
  if (typeof host?.newChat !== 'function') missing.push('host.newChat')
  if (typeof host?.profiles?.list !== 'function') missing.push('host.profiles.list')
  if (!host?.state?.focusedStoredSessionId && !host?.state?.activeSessionId) missing.push('host.state.*SessionId')
  return missing
}

const sessionAtom = () => host.state.focusedStoredSessionId || host.state.activeSessionId
const profileAtom = () => host.state.focusedSessionProfile || host.state.profile

// ---------------------------------------------------------------------------
// Data
// ---------------------------------------------------------------------------

function normalize(name) {
  return String(name ?? '').trim().toLowerCase() || 'default'
}

function labelOf(p) {
  return (p && (p.display_name || p.bot_title || p.name)) || 'default'
}

function sortProfiles(list) {
  // default first, then alphabetical
  return [...list].sort((a, b) => {
    if (Boolean(a.is_default) !== Boolean(b.is_default)) return a.is_default ? -1 : 1
    return labelOf(a).localeCompare(labelOf(b))
  })
}

async function loadProfiles() {
  const res = await host.profiles.list()
  return sortProfiles(Array.isArray(res?.profiles) ? res.profiles : [])
}

function useProfiles() {
  return useQuery({
    queryKey: ['plugin', ID, 'profiles'],
    queryFn: loadProfiles,
    staleTime: 60_000,
    refetchOnWindowFocus: false
  })
}

/** Route descriptor for the profile on the current connection (works with remote backends). */
async function routeFor(name) {
  if (typeof host.profileRoutes !== 'function') return null
  try {
    const routes = await host.profileRoutes()
    const key = normalize(name)
    const own = (routes || []).filter(r => normalize(r.profile) === key)
    return own.find(r => r.primary) || own[0] || null
  } catch {
    return null
  }
}

async function readDraft() {
  if (typeof host.composer?.getDraft !== 'function') return ''
  try {
    return (await host.composer.getDraft('new')) ?? (await host.composer.getDraft(null)) ?? ''
  } catch {
    return ''
  }
}

/** Write the carried-over text into the new draft once its composer is mounted. */
function restoreDraft(text) {
  if (!text || !text.trim() || typeof host.composer?.setDraft !== 'function') return
  let tries = 0
  const attempt = async () => {
    tries += 1
    let ok = false
    try {
      ok = await host.composer.setDraft('new', text)
    } catch {
      ok = false
    }
    if (!ok && tries < 12) setTimeout(attempt, 250)
  }
  setTimeout(attempt, 150)
}

async function switchTo(profile, current) {
  if (normalize(profile.name) === normalize(current)) return
  const text = await readDraft()
  const route = await routeFor(profile.name)
  try {
    host.newChat(route || profile.name)
  } catch (error) {
    host.notifyError?.(error, translate('openFailed', labelOf(profile)))
    return
  }
  restoreDraft(text)
}

// ---------------------------------------------------------------------------
// UI
// ---------------------------------------------------------------------------

function Dot({ name }) {
  const color = profileColor(name)
  return jsx('span', {
    'aria-hidden': true,
    className: 'inline-block size-2 shrink-0 rounded-full',
    style: { background: color || 'var(--ui-text-quaternary)' }
  })
}

const pillClass =
  'inline-flex h-7 max-w-44 items-center gap-1.5 rounded-full border border-(--ui-stroke-secondary) px-2.5 ' +
  'text-[0.75rem] leading-none text-(--ui-text-secondary) transition-colors'

function ProfilePicker() {
  const t = useT()
  const sessionId = useValue(sessionAtom())
  const current = normalize(useValue(profileAtom()))
  const { data: profiles } = useProfiles()

  if (!profiles || profiles.length < 2) return null

  const active = profiles.find(p => normalize(p.name) === current) || { name: current }

  // Running chat: read-only chip
  if (sessionId) {
    return jsx(Tip, {
      label: t('chipTip'),
      children: jsxs('span', {
        className: pillClass + ' cursor-default opacity-80',
        'data-plugin': ID,
        children: [jsx(Dot, { name: active.name }), jsx('span', { className: 'truncate', children: labelOf(active) })]
      })
    })
  }

  // New draft: dropdown
  return jsxs(DropdownMenu, {
    children: [
      jsx(DropdownMenuTrigger, {
        asChild: true,
        children: jsxs('button', {
          type: 'button',
          'data-plugin': ID,
          'aria-label': t('pickerAria'),
          title: t('pickerAria'),
          className: pillClass + ' hover:bg-(--chrome-action-hover) hover:text-foreground',
          children: [
            jsx(Dot, { name: active.name }),
            jsx('span', { className: 'truncate', children: t('pickerLabel', labelOf(active)) }),
            jsx('span', { 'aria-hidden': true, className: 'text-(--ui-text-quaternary)', children: '▾' })
          ]
        })
      }),
      jsxs(DropdownMenuContent, {
        align: 'start',
        side: 'top',
        className: 'min-w-52',
        children: [
          jsx('div', {
            className: 'px-2 py-1.5 text-[0.6875rem] text-(--ui-text-tertiary)',
            children: t('menuHeader')
          }),
          jsx(DropdownMenuSeparator, {}),
          ...profiles.map(p =>
            jsxs(
              DropdownMenuItem,
              {
                onSelect: () => void switchTo(p, current),
                className: 'gap-2',
                children: [
                  jsx(Dot, { name: p.name }),
                  jsx('span', { className: 'flex-1 truncate', children: labelOf(p) }),
                  normalize(p.name) === current
                    ? jsx('span', { className: 'text-(--ui-text-tertiary)', children: '✓' })
                    : null
                ]
              },
              p.name
            )
          )
        ]
      })
    ]
  })
}

// ---------------------------------------------------------------------------
// Registration
// ---------------------------------------------------------------------------

export default {
  id: ID,
  name: 'Profile Picker',
  register(ctx) {
    if (ctx.i18n && typeof ctx.i18n.register === 'function') {
      ctx.i18n.register(MESSAGES)
      if (typeof ctx.i18n.t === 'function') translate = (key, ...args) => ctx.i18n.t(key, ...args)
    }

    const missing = missingCapabilities()
    if (missing.length) {
      console.warn(`[${ID}] inactive, missing plugin APIs: ${missing.join(', ')}`)
      host.notify?.({
        kind: 'warning',
        title: translate('inactiveTitle'),
        message: translate('inactiveMessage'),
        detail: missing.join(', ')
      })
      return
    }

    ctx.register({
      id: 'picker',
      area: COMPOSER_AREAS.leading,
      order: 10,
      render: () => jsx(ProfilePicker, {})
    })

    // Command palette: one row per profile. Labels are resolved when the
    // palette reads them, so they follow the app language.
    if (PALETTE_AREA) {
      loadProfiles()
        .then(list => {
          if (list.length < 2) return
          ctx.registerMany(
            list.map(p => {
              const data = {
                id: `${ID}:new-in-${normalize(p.name)}`,
                keywords: ['profile', 'profil', 'new chat', 'neue session', p.name],
                run: () => void switchTo(p, '__none__')
              }
              Object.defineProperty(data, 'label', {
                enumerable: true,
                get: () => translate('paletteNew', labelOf(p))
              })
              return { id: `new-in-${normalize(p.name)}`, area: PALETTE_AREA, data }
            })
          )
        })
        .catch(() => {})
    }

    console.info(`[${ID}] v${VERSION} loaded`)
  }
}
