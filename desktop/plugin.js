/**
 * Next Prompt — desktop half.
 *
 * After a turn finishes, polls this plugin's Python backend (through
 * `ctx.rest`, which routes to the right backend and carries auth) for an
 * AI-generated follow-up and shows it as a subtle pill below the composer.
 * Click → the text is placed in the composer (not sent). × → dismiss.
 */

import { atom, cn, Codicon, haptic, host, Tip, usePluginI18n, useQuery, useValue } from '@hermes/plugin-sdk'
import { useEffect } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'next-prompt'

/** Set in register(); components read it (ctx.rest is plugin-scoped). */
let pluginCtx = null

/** `${storedSessionId}:${timestamp}` the user dismissed or used. */
const $handled = atom(null)
/** storedSessionId → last epoch ms that chat was seen working. Data fetched
 *  before then predates the turn and waits for a fresh fetch. The backend is
 *  the authority on "still pending" (it drops the suggestion the moment the
 *  user sends their own message), so this only guards the local cache. */
const busySeenAt = new Map()
/** Epoch ms when the focused chat last went idle — drives poll cadence. */
let idleSinceMs = 0
let reportedError = false

const FAST_POLL_MS = 2000
const SLOW_POLL_MS = 20000
const FAST_WINDOW_MS = 45000

// ── composer insertion ───────────────────────────────────────────────────
// The SDK has no public "insert into composer" door yet. The composer listens
// on a window event bus (app/chat/composer/focus.ts); target the visible one.
function visibleComposerTarget() {
  if (typeof document === 'undefined') return null
  const surfaces = Array.from(document.querySelectorAll('[data-composer-target]'))
  const visible = surfaces.find(el => !el.closest('[data-pane-hidden]')) || surfaces[0]
  return visible ? visible.getAttribute('data-composer-target') : null
}

function insertIntoComposer(text) {
  const target = visibleComposerTarget()
  if (!target) return false
  window.dispatchEvent(new CustomEvent('hermes:composer-insert', { detail: { mode: 'block', target, text } }))
  window.setTimeout(
    () => window.dispatchEvent(new CustomEvent('hermes:composer-focus', { detail: { target } })),
    0
  )
  return true
}

// ── backend ──────────────────────────────────────────────────────────────

async function fetchSuggestion(storedId) {
  const data = await pluginCtx.rest(`/suggestion?session_id=${encodeURIComponent(storedId)}`)
  return (data && data.suggestion) || null
}

function dismissOnServer(storedId) {
  if (!storedId || !pluginCtx) return
  pluginCtx.rest(`/dismiss?session_id=${encodeURIComponent(storedId)}`, { method: 'POST' }).catch(() => {})
}

// ── UI ───────────────────────────────────────────────────────────────────

function SuggestionStrip() {
  const t = usePluginI18n(ID)
  const storedId = useValue(host.state.focusedStoredSessionId)
  const busy = useValue(host.state.busy)
  const handled = useValue($handled)

  // Per session, never global: another chat working (or switching screens)
  // must not hide this chat's suggestion.
  if (storedId && busy) busySeenAt.set(storedId, Date.now())

  useEffect(() => {
    if (!busy) idleSinceMs = Date.now()
  }, [busy])

  const query = useQuery({
    queryKey: ['next-prompt', 'suggestion', storedId],
    queryFn: () => fetchSuggestion(storedId),
    enabled: Boolean(pluginCtx && storedId && !busy),
    refetchInterval: () => (Date.now() - idleSinceMs < FAST_WINDOW_MS ? FAST_POLL_MS : SLOW_POLL_MS),
    refetchOnMount: 'always',
    refetchOnWindowFocus: true,
    retry: false
  })

  useEffect(() => {
    if (query.error && !reportedError) {
      reportedError = true
      host.notifyError(query.error, 'Next Prompt could not reach its backend')
    }
  }, [query.error])

  const suggestion = query.data
  if (busy || !storedId || !suggestion || !suggestion.text) return null
  if (suggestion.session_id && suggestion.session_id !== storedId) return null
  if (query.dataUpdatedAt <= (busySeenAt.get(storedId) || 0)) return null

  const key = `${storedId}:${suggestion.timestamp}`
  if (handled === key) return null

  const finish = () => {
    $handled.set(key)
    dismissOnServer(storedId)
  }

  const onUse = () => {
    haptic('tap')
    if (!insertIntoComposer(suggestion.text) && pluginCtx) {
      void pluginCtx.os.writeClipboard(suggestion.text)
      host.notify({ kind: 'info', message: t('copied') })
    }
    finish()
  }

  const onDismiss = event => {
    event.stopPropagation()
    finish()
  }

  return jsx('div', {
    className: 'flex min-w-0 items-center justify-start',
    children: jsx(Tip, {
      label: t('tip'),
      children: jsxs('button', {
        type: 'button',
        className: cn(
          'group/np flex min-w-0 max-w-full items-center gap-1.5 rounded-full',
          'border border-(--ui-stroke-secondary) px-3 py-1',
          'text-[0.8125rem] text-(--ui-text-secondary)',
          'transition-colors duration-150 hover:border-(--ui-accent) hover:text-(--ui-accent)'
        ),
        onClick: onUse,
        children: [
          jsx(Codicon, { name: 'sparkle', className: 'shrink-0 text-[0.75rem] opacity-70' }),
          jsx('span', { className: 'truncate', children: suggestion.text }),
          jsx('span', {
            role: 'button',
            'aria-label': t('dismiss'),
            className: 'ml-1 shrink-0 rounded-full p-0.5 opacity-50 hover:opacity-100',
            onClick: onDismiss,
            children: jsx(Codicon, { name: 'close', className: 'text-[0.625rem]' })
          })
        ]
      })
    })
  })
}

// ── plugin export ────────────────────────────────────────────────────────

export default {
  id: ID,
  name: 'Next Prompt',
  defaultEnabled: false, // opt-in in Settings → Plugins

  register(ctx) {
    pluginCtx = ctx

    ctx.i18n.register({
      en: {
        tip: 'Click to put this follow-up in the composer',
        dismiss: 'Dismiss suggestion',
        copied: 'Suggestion copied — paste it into the composer'
      },
      es: {
        tip: 'Clic para poner este seguimiento en el compositor',
        dismiss: 'Descartar sugerencia',
        copied: 'Sugerencia copiada — pégala en el compositor'
      }
    })

    ctx.register({
      id: 'suggestion-strip',
      area: 'composer.underside',
      order: 100,
      render: () => jsx(SuggestionStrip, {})
    })

    ctx.onDispose(() => {
      pluginCtx = null
      reportedError = false
    })
  }
}
