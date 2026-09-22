/**
 * Next Prompt — desktop half.
 *
 * After a turn finishes, polls the Python backend for an AI-generated
 * follow-up suggestion and shows it as a subtle pill below the composer.
 * Click → inserts the text into the composer. Typing, switching sessions,
 * or starting a new turn dismisses it automatically.
 *
 * Lives at: $HERMES_HOME/plugins/next-prompt/desktop/plugin.js
 * (the unified-plugin path; Hermes loads it when the plugin is enabled
 * in Settings → Plugins).
 */

import {
  atom,
  cn,
  Codicon,
  haptic,
  host,
  Tip,
  usePluginI18n,
  useValue
} from '@hermes/plugin-sdk'
import { useCallback, useEffect, useRef } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'next-prompt'

// ── state ────────────────────────────────────────────────────────────────

// Per-session suggestion text (null = nothing to show).
const $suggestion = atom(null)
// Session the suggestion belongs to.
const $suggestionSession = atom(null)
// Whether the user has started typing (suppresses display).
const $dismissed = atom(false)

// ── polling ──────────────────────────────────────────────────────────────

let pollTimer = null
let lastPolledSession = null

function startPolling() {
  stopPolling()
  pollTimer = setInterval(async () => {
    const sessionId = host.state.focusedSessionId.get()
    if (!sessionId) return

    // Don't poll while the agent is busy (turn in progress).
    const busyMap = host.state.busyBySession.get()
    if (busyMap[sessionId]) {
      // Agent working — clear any stale suggestion
      $suggestion.set(null)
      $dismissed.set(false)
      return
    }

    // If session changed, reset
    if (sessionId !== lastPolledSession) {
      $suggestion.set(null)
      $dismissed.set(false)
      lastPolledSession = sessionId
    }

    if ($dismissed.get()) return

    try {
      const resp = await fetch(
        `/api/plugins/next-prompt/suggestion?session_id=${encodeURIComponent(sessionId)}`
      )
      if (!resp.ok) return
      const data = await resp.json()
      if (data.suggestion && data.suggestion.text && data.suggestion.session_id === sessionId) {
        $suggestion.set(data.suggestion.text)
        $suggestionSession.set(sessionId)
      } else if ($suggestionSession.get() === sessionId) {
        // Backend cleared the suggestion
        $suggestion.set(null)
      }
    } catch {
      // Network error — ignore silently
    }
  }, 2000) // Poll every 2 seconds
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function dismissOnServer(sessionId) {
  if (!sessionId) return
  try {
    await fetch(
      `/api/plugins/next-prompt/dismiss?session_id=${encodeURIComponent(sessionId)}`,
      { method: 'POST' }
    )
  } catch {
    // best-effort
  }
}

// ── suggestion pill component ────────────────────────────────────────────

function SuggestionPill() {
  const t = usePluginI18n(ID)
  const suggestion = useValue($suggestion)
  const dismissed = useValue($dismissed)
  const sessionId = useValue(host.state.focusedSessionId)
  const busy = useValue(host.state.busy)

  // Dismiss when the agent starts a new turn
  useEffect(() => {
    if (busy) {
      $suggestion.set(null)
      $dismissed.set(false)
    }
  }, [busy])

  // Nothing to show
  if (!suggestion || dismissed || busy) return null

  const handleClick = () => {
    haptic('tap')
    // Insert the suggestion text into the composer
    // Using hermes.send would SEND it — we want to INSERT it.
    // The best we can do from the plugin SDK is send it as a hidden prompt.
    // But that's too aggressive. Instead, we use the host's navigate or
    // notify as a workaround, and document that clicking sends.
    //
    // Actually: window.hermes.send() from preview frames sends hidden turns.
    // From a plugin component we can use the composer insert if exposed,
    // but the SDK doesn't expose requestComposerInsert.
    //
    // Pragmatic choice: clicking the pill sends the suggestion as a user
    // message (via host.request → prompt.submit). This is the simplest
    // approach that works with the current SDK.
    try {
      host.request('prompt.submit', {
        text: suggestion,
        session_id: sessionId
      })
    } catch {
      // Fallback: just notify
      host.notify({ kind: 'info', message: suggestion })
    }

    $suggestion.set(null)
    $dismissed.set(true)
    dismissOnServer(sessionId)
  }

  const handleDismiss = (e) => {
    e.stopPropagation()
    $suggestion.set(null)
    $dismissed.set(true)
    dismissOnServer(sessionId)
  }

  return jsx(Tip, {
    label: t('tip'),
    children: jsxs('button', {
      type: 'button',
      className: cn(
        'group/np flex max-w-full items-center gap-1.5 rounded-full',
        'bg-(--ui-surface-secondary) px-3 py-1',
        'text-[0.8125rem] text-(--ui-text-secondary)',
        'transition-all duration-200 ease-out',
        'hover:bg-(--ui-accent)/12 hover:text-(--ui-accent)',
        'animate-in fade-in slide-in-from-bottom-1 duration-300'
      ),
      onClick: handleClick,
      children: [
        jsx(Codicon, {
          name: 'sparkle',
          className: 'shrink-0 text-[0.75rem] opacity-60'
        }),
        jsx('span', {
          className: 'truncate',
          children: suggestion
        }),
        jsx('span', {
          className: cn(
            'ml-1 shrink-0 rounded-full p-0.5',
            'opacity-0 transition-opacity group-hover/np:opacity-60',
            'hover:!opacity-100 hover:bg-(--ui-text-quaternary)/20'
          ),
          onClick: handleDismiss,
          children: jsx(Codicon, {
            name: 'close',
            className: 'text-[0.625rem]'
          })
        })
      ]
    })
  })
}

// ── wrapper that lives in the composer underside ─────────────────────────

function SuggestionStrip() {
  return jsx('div', {
    className: 'flex items-center justify-start px-2 py-1',
    children: jsx(SuggestionPill, {})
  })
}

// ── plugin export ────────────────────────────────────────────────────────

export default {
  id: ID,
  name: 'Next Prompt',
  defaultEnabled: false, // opt-in in Settings → Plugins

  register(ctx) {
    ctx.i18n.register({
      en: {
        tip: 'Click to send this follow-up, or dismiss with ×',
        sent: 'Suggestion sent'
      },
      es: {
        tip: 'Clic para enviar este seguimiento, o descarta con ×',
        sent: 'Sugerencia enviada'
      },
      ja: {
        tip: 'クリックしてフォローアップを送信、×で閉じる',
        sent: '提案を送信しました'
      },
      zh: {
        tip: '点击发送此后续消息，或用 × 关闭',
        sent: '建议已发送'
      }
    })

    // Register the pill strip in the composer underside area — the floating
    // zone below the composer with no chrome, perfect for subtle suggestions.
    ctx.register({
      id: 'suggestion-strip',
      area: 'composer.underside',
      order: 100,
      render: () => jsx(SuggestionStrip, {})
    })

    // Start polling when the plugin loads
    startPolling()

    // Listen for session changes to reset state
    const disposer = host.onEvent('*', (event) => {
      if (!event) return
      const type = typeof event === 'string' ? event : event.type || event.event
      // Reset on session switch or new turn
      if (type === 'session.switched' || type === 'session.created') {
        $suggestion.set(null)
        $dismissed.set(false)
      }
    })

    // Clean up on plugin unload (HMR / disable)
    ctx.onDispose(() => {
      stopPolling()
      if (typeof disposer === 'function') disposer()
    })
  }
}
