/**
 * Next Prompt — desktop half.
 *
 * After a turn finishes, polls this plugin's Python backend (through
 * `ctx.rest`, which routes to the right backend and carries auth) for an
 * AI-generated follow-up and shows it as a subtle pill below the composer.
 * Click → the text goes into the composer (not sent) through the SDK's
 * `host.composer`, or to the clipboard on hosts without it. × → dismiss.
 * When the suggestion could not be generated, a muted note says why instead.
 */

import { atom, cn, Codicon, haptic, host, Tip, usePluginI18n, useQuery, useValue } from '@hermes/plugin-sdk'
import { useEffect } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'next-prompt'

/** Set in register(); components read it (ctx.rest is plugin-scoped). */
let pluginCtx = null

/** `${storedSessionId}:${kind}:${timestamp}` the user dismissed or used. */
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
const FAILURE_KINDS = new Set(['auth', 'rate_limit', 'timeout', 'other'])

// ── composer insertion ───────────────────────────────────────────────────
// SDK only. `host.composer.insertText` (Desktop plugin SDK hook 1, #116305)
// places the text in the focused composer without sending it and focuses the
// caret; it resolves false when no composer can take it. Hosts without the
// hook fall back to the clipboard. The app's composer DOM and internal event
// bus are never touched.
async function insertIntoComposer(text) {
  const composer = host.composer
  if (!composer || typeof composer.insertText !== 'function') return false
  try {
    return (await composer.insertText(null, text, { mode: 'block' })) === true
  } catch {
    return false
  }
}

// ── backend ──────────────────────────────────────────────────────────────

/** `{ suggestion, error }` for one session (see dashboard/plugin_api.py). */
async function fetchState(storedId) {
  const data = await pluginCtx.rest(`/suggestion?session_id=${encodeURIComponent(storedId)}`)
  return { suggestion: (data && data.suggestion) || null, error: (data && data.error) || null }
}

function dismissOnServer(storedId) {
  if (!storedId || !pluginCtx) return
  pluginCtx.rest(`/dismiss?session_id=${encodeURIComponent(storedId)}`, { method: 'POST' }).catch(() => {})
}

// ── UI ───────────────────────────────────────────────────────────────────

const rowClass = 'flex min-w-0 items-center justify-start'
const closeClass = 'ml-1 shrink-0 rounded-full p-0.5 opacity-50 hover:opacity-100'

function SuggestionPill({ storedId, suggestion, handledKey }) {
  const t = usePluginI18n(ID)

  const finish = () => {
    $handled.set(handledKey)
    dismissOnServer(storedId)
  }

  const onUse = async () => {
    haptic('tap')
    const text = suggestion.text
    finish()
    if (await insertIntoComposer(text)) return
    if (!pluginCtx) return
    try {
      await pluginCtx.os.writeClipboard(text)
      host.notify({ kind: 'info', message: t('copied') })
    } catch (error) {
      host.notifyError(error, t('copyFailed'))
    }
  }

  const onDismiss = event => {
    event.stopPropagation()
    finish()
  }

  return jsx('div', {
    className: rowClass,
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
            className: closeClass,
            onClick: onDismiss,
            children: jsx(Codicon, { name: 'close', className: 'text-[0.625rem]' })
          })
        ]
      })
    })
  })
}

/** Muted, dismissible note in the pill's place when generation failed. */
function FailureNote({ storedId, failure, handledKey }) {
  const t = usePluginI18n(ID)
  const kind = FAILURE_KINDS.has(failure.kind) ? failure.kind : 'other'

  const onDismiss = () => {
    $handled.set(handledKey)
    dismissOnServer(storedId)
  }

  return jsx('div', {
    className: rowClass,
    children: jsx(Tip, {
      label: t(`failure.${kind}Hint`),
      children: jsxs('div', {
        role: 'status',
        className: cn(
          'flex min-w-0 max-w-full items-center gap-1.5 rounded-full px-3 py-1',
          'text-[0.8125rem] text-(--ui-text-tertiary)'
        ),
        children: [
          jsx(Codicon, { name: 'warning', className: 'shrink-0 text-[0.75rem] opacity-70' }),
          jsx('span', { className: 'truncate', children: t(`failure.${kind}`) }),
          jsx('button', {
            type: 'button',
            'aria-label': t('dismiss'),
            className: closeClass,
            onClick: onDismiss,
            children: jsx(Codicon, { name: 'close', className: 'text-[0.625rem]' })
          })
        ]
      })
    })
  })
}

function SuggestionStrip() {
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
    queryFn: () => fetchState(storedId),
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

  if (busy || !storedId || !query.data) return null
  if (query.dataUpdatedAt <= (busySeenAt.get(storedId) || 0)) return null

  const { suggestion, error: failure } = query.data
  const mine = item => item && (!item.session_id || item.session_id === storedId)

  if (mine(suggestion) && suggestion.text) {
    const handledKey = `${storedId}:suggestion:${suggestion.timestamp}`
    if (handled === handledKey) return null
    return jsx(SuggestionPill, { storedId, suggestion, handledKey })
  }

  if (mine(failure)) {
    const handledKey = `${storedId}:failure:${failure.timestamp}`
    if (handled === handledKey) return null
    return jsx(FailureNote, { storedId, failure, handledKey })
  }

  return null
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
        tip: 'Click to use this follow-up',
        dismiss: 'Dismiss suggestion',
        copied: 'Suggestion copied — paste it into the composer',
        copyFailed: 'Could not copy the suggestion',
        failure: {
          auth: 'Next Prompt: the model rejected its credentials',
          authHint:
            'Check the provider login (hermes auth list), or fully quit and reopen Hermes Desktop. You can also point auxiliary.next_prompt at another provider in config.yaml.',
          rate_limit: 'Next Prompt: the provider is rate-limiting requests',
          rate_limitHint: 'It tries again after your next message.',
          timeout: 'Next Prompt: the model took too long',
          timeoutHint: 'It tries again after your next message.',
          other: 'Next Prompt could not generate a suggestion',
          otherHint: 'Details are in the Hermes logs (hermes logs --level WARNING).'
        }
      },
      es: {
        tip: 'Clic para usar este seguimiento',
        dismiss: 'Descartar sugerencia',
        copied: 'Sugerencia copiada — pégala en el compositor',
        copyFailed: 'No se pudo copiar la sugerencia',
        failure: {
          auth: 'Next Prompt: el modelo rechazó sus credenciales',
          authHint:
            'Revisa el login del proveedor (hermes auth list) o cierra Hermes Desktop por completo, también desde la bandeja, y vuelve a abrirlo. También puedes apuntar auxiliary.next_prompt a otro proveedor en config.yaml.',
          rate_limit: 'Next Prompt: el proveedor está limitando las peticiones',
          rate_limitHint: 'Lo intentará de nuevo tras tu próximo mensaje.',
          timeout: 'Next Prompt: el modelo tardó demasiado',
          timeoutHint: 'Lo intentará de nuevo tras tu próximo mensaje.',
          other: 'Next Prompt no pudo generar una sugerencia',
          otherHint: 'Los detalles están en los logs de Hermes (hermes logs --level WARNING).'
        }
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
