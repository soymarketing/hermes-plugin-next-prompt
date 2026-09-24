// Minimal stand-in for `@hermes/plugin-sdk` — only what desktop/plugin.js
// imports. It proves behaviour, not that the real SDK exports exist; CI greps
// the imported names separately (see tests/frontend/plugin.test.mjs).

export const calls = { insert: [], clip: [], notify: [], err: [] }

const mk = value => {
  let current = value
  const subs = new Set()
  return {
    get: () => current,
    set: next => {
      current = next
      subs.forEach(fn => fn())
    },
    subscribe: fn => (subs.add(fn), () => subs.delete(fn))
  }
}

export const atom = mk
export const cn = (...parts) => parts.filter(Boolean).join(' ')
export const Codicon = () => null
export const Tip = props => props.children
export const haptic = () => {}
export const KEYBINDS_AREA = 'keybinds'
export const PALETTE_AREA = 'palette'

export const host = {
  state: { focusedStoredSessionId: mk(null), busy: mk(false) },
  notify: message => calls.notify.push(message),
  notifyError: (error, message) => calls.err.push(message)
}

export const usePluginI18n = () => key => key
export const useValue = a => a.get()

// Fake React Query: one cached result per stored session id, like the real
// backend answering GET /suggestion for that session.
export const server = new Map()
export const queries = { last: null }
export const useQuery = opts => {
  queries.last = opts
  return server.get(opts.queryKey[2]) || { data: null, dataUpdatedAt: 0, error: null }
}
