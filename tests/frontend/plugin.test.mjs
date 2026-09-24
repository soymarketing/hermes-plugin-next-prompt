// Behaviour of the Desktop half (desktop/plugin.js) against a stub SDK.
// Run: node --test tests/frontend/

import assert from 'node:assert/strict'
import { test } from 'node:test'

import { contribution, expand, find, loadPlugin, makeCtx, PLUGIN_SOURCE, resetCalls, serve, sleep, textOf, view } from './harness.mjs'
import { calls, host, queries } from './stubs/sdk.mjs'

const plugin = await loadPlugin()
const ctx = makeCtx()
plugin.register(ctx)
const strip = contribution(ctx, 'composer.underside')

let stamp = 0
const suggestion = (session_id, text = 'Sigue con el deploy') => ({ text, session_id, timestamp: ++stamp })
const render = () => expand(strip.render())
const pill = () => find(render(), n => n.type === 'button' && n.props?.['aria-label'] !== 'dismiss')
const pillText = () => {
  const button = pill()
  return button ? textOf(button).trim() : null
}
const closeButton = () => find(render(), n => n.props?.role === 'button' || n.props?.['aria-label'] === 'dismiss')
const note = () => find(render(), n => n.props?.role === 'status')
const failure = (session_id, kind) => ({ kind, session_id, timestamp: ++stamp })

test('only imports the SDK, react and react/jsx-runtime', () => {
  const specifiers = [...PLUGIN_SOURCE.matchAll(/^import .* from '([^']+)'/gm)].map(m => m[1])
  assert.deepEqual([...new Set(specifiers)].sort(), ['@hermes/plugin-sdk', 'react', 'react/jsx-runtime'])
})

test('never reaches into app DOM or internal events (catalog rule 8)', () => {
  for (const banned of ['document.', 'querySelector', 'dispatchEvent', 'CustomEvent', 'localStorage', 'hermes:composer']) {
    assert.ok(!PLUGIN_SOURCE.includes(banned), `plugin.js must not use ${banned}`)
  }
})

test('ships opt-in and contributes to composer.underside', () => {
  assert.equal(plugin.id, 'next-prompt')
  assert.equal(plugin.defaultEnabled, false)
  assert.ok(strip && typeof strip.render === 'function')
})

test('shows the pill for the focused chat', () => {
  view('A')
  serve('A', suggestion('A'))
  assert.equal(pillText(), 'Sigue con el deploy')
})

test('another chat working does not hide this chat’s pill', async () => {
  view('A')
  serve('A', suggestion('A', 'Corre los tests'))
  assert.equal(pillText(), 'Corre los tests')
  await sleep(5)
  view('B', true)
  assert.equal(pillText(), null, 'nothing on the busy chat B')
  await sleep(5)
  view('B')
  assert.equal(pillText(), null, 'B has no suggestion of its own')
  view('A')
  assert.equal(pillText(), 'Corre los tests', 'back on A: still there')
  view(null)
  assert.equal(pillText(), null, 'non-chat screen shows nothing')
  view('A')
  assert.equal(pillText(), 'Corre los tests')
})

test('re-checks the backend on mount and pauses polling while busy', () => {
  view('A')
  render()
  assert.equal(queries.last.refetchOnMount, 'always')
  assert.equal(queries.last.enabled, true)
  view('A', true)
  render()
  assert.equal(queries.last.enabled, false)
  view(null)
  render()
  assert.equal(queries.last.enabled, false)
})

test('a new turn in this chat hides the old pill until a fresh one arrives', async () => {
  view('A')
  serve('A', suggestion('A', 'Viejo'))
  assert.equal(pillText(), 'Viejo')
  await sleep(5)
  view('A', true)
  assert.equal(pillText(), null)
  await sleep(5)
  view('A')
  assert.equal(pillText(), null, 'cached pill from before the turn is not resurrected')
  serve('A', suggestion('A', 'Nuevo'))
  assert.equal(pillText(), 'Nuevo')
})

test('a suggestion for another session is never shown', () => {
  view('C')
  serve('C', suggestion('X'))
  assert.equal(pillText(), null)
})

test('click inserts through host.composer.insertText when the host has it', async () => {
  resetCalls()
  host.composer = { insertText: async (sid, text, opts) => (calls.insert.push([sid, text, opts]), true) }
  view('A')
  serve('A', suggestion('A', 'Inserta esto'))
  await pill().props.onClick()
  assert.deepEqual(calls.insert, [[null, 'Inserta esto', { mode: 'block' }]])
  assert.deepEqual(calls.clip, [])
  assert.equal(pillText(), null, 'used suggestion disappears')
  assert.deepEqual(ctx.restCalls.at(-1), ['/dismiss?session_id=A', 'POST'])
  delete host.composer
})

test('click falls back to the clipboard when insertion is unavailable', async () => {
  const hosts = [undefined, { insertText: async () => false }, { insertText: async () => { throw new Error('x') } }, { insertText: 'nope' }]
  for (const composer of hosts) {
    resetCalls()
    if (composer) host.composer = composer
    else delete host.composer
    view('A')
    serve('A', suggestion('A', 'Copia esto'))
    await pill().props.onClick()
    assert.deepEqual(calls.clip, ['Copia esto'])
    assert.equal(calls.notify[0]?.message, 'copied')
  }
  delete host.composer
})

test('a clipboard failure is reported, not swallowed', async () => {
  resetCalls()
  const original = ctx.os.writeClipboard
  ctx.os.writeClipboard = async () => {
    throw new Error('denied')
  }
  view('A')
  serve('A', suggestion('A'))
  await pill().props.onClick()
  assert.deepEqual(calls.err, ['copyFailed'])
  ctx.os.writeClipboard = original
})

test('× dismisses without using the text', () => {
  resetCalls()
  view('A')
  serve('A', suggestion('A', 'Descártame'))
  let stopped = false
  closeButton().props.onClick({ stopPropagation: () => (stopped = true) })
  assert.ok(stopped, 'the click does not reach the pill')
  assert.equal(pillText(), null)
  assert.deepEqual(calls.clip, [])
  assert.deepEqual(ctx.restCalls.at(-1), ['/dismiss?session_id=A', 'POST'])
})

test('a failed generation shows a muted note that names the cause', () => {
  for (const kind of ['auth', 'rate_limit', 'timeout', 'other']) {
    view('A')
    serve('A', null, failure('A', kind))
    assert.equal(pill(), null, 'no pill for a failure')
    assert.equal(textOf(note()).trim(), `failure.${kind}`)
  }
})

test('an unknown failure kind falls back to the generic note', () => {
  view('A')
  serve('A', null, failure('A', 'something-new'))
  assert.equal(textOf(note()).trim(), 'failure.other')
})

test('the failure note can be dismissed and stays dismissed', () => {
  view('A')
  serve('A', null, failure('A', 'auth'))
  closeButton().props.onClick()
  assert.equal(note(), null)
  assert.deepEqual(ctx.restCalls.at(-1), ['/dismiss?session_id=A', 'POST'])
  render()
  assert.equal(note(), null)
})

test('a suggestion wins over a failure note', () => {
  view('A')
  serve('A', suggestion('A', 'Gana la sugerencia'), failure('A', 'auth'))
  assert.equal(pillText(), 'Gana la sugerencia')
  assert.equal(note(), null)
})

test('a failure for another session is never shown', () => {
  view('A')
  serve('A', null, failure('Z', 'auth'))
  assert.equal(note(), null)
})

test('the failure note is hidden while the chat works', async () => {
  view('A')
  serve('A', null, failure('A', 'timeout'))
  assert.ok(note())
  await sleep(5)
  view('A', true)
  assert.equal(note(), null)
})

// ── keybind + palette ─────────────────────────────────────────────────────

const byArea = area => ctx.contributions.filter(c => c.area === area).map(c => c.data)
const keybind = () => byArea('keybinds').find(k => k.id === 'next-prompt.use')
const command = id => byArea('palette').find(p => p.id === id)

test('registers a rebindable keybind and palette commands', () => {
  const k = keybind()
  assert.ok(k, 'keybind registered')
  assert.deepEqual(k.defaults, ['mod+shift+y'])
  assert.equal(k.category, 'composer')
  assert.equal(typeof k.label, 'string')
  assert.equal(command('next-prompt.use').action, 'next-prompt.use', 'palette row shows the live combo')
  assert.ok(command('next-prompt.dismiss'))
})

test('the keybind default does not collide with a built-in desktop binding', () => {
  // Built-in defaults as of Desktop 0.21 (lib/keybinds/actions.ts + read-only composer keys).
  const taken = new Set([
    'mod+shift+m', 'mod+shift+]', 'mod+shift+[', 'mod+shift+0', 'mod+shift+n', 'mod+shift+f', 'mod+shift+b',
    'mod+shift+s', 'mod+shift+l', 'mod+shift+h', 'mod+shift+t', 'mod+shift+k', 'mod+shift+c', 'mod+shift+g',
    'mod+shift+v', 'mod+enter', 'shift+enter', 'mod+k', 'mod+p', 'mod+l', 'mod+z', 'mod+y', 'ctrl+y', 'mod+shift+z'
  ])
  for (const combo of keybind().defaults) assert.ok(!taken.has(combo), `${combo} is taken`)
})

test('the keybind uses the suggestion shown in the focused chat', async () => {
  resetCalls()
  host.composer = { insertText: async (sid, text, opts) => (calls.insert.push([sid, text, opts]), true) }
  view('A')
  serve('A', suggestion('A', 'Por atajo'))
  render()
  assert.equal(command('next-prompt.use').detail(), 'Por atajo', 'palette row previews the text')
  keybind().run()
  await sleep(1)
  assert.deepEqual(calls.insert, [[null, 'Por atajo', { mode: 'block' }]])
  assert.equal(pillText(), null, 'used suggestion disappears')
  delete host.composer
})

test('the keybind never acts on another chat’s suggestion', async () => {
  resetCalls()
  view('A')
  serve('A', suggestion('A', 'De A'))
  render()
  view('B') // switched chats; no render yet
  keybind().run()
  await sleep(1)
  assert.deepEqual(calls.clip, [])
  assert.deepEqual(calls.insert, [])
  assert.equal(calls.notify.at(-1)?.message, 'nothingToUse')
})

test('the keybind does nothing while the chat works, or with no suggestion', async () => {
  resetCalls()
  view('A')
  serve('A', suggestion('A', 'Ocupado'))
  render()
  host.state.busy.set(true)
  keybind().run()
  host.state.busy.set(false)
  view('C')
  serve('C', null)
  render()
  keybind().run()
  await sleep(1)
  assert.deepEqual(calls.clip, [])
  assert.equal(calls.notify.filter(n => n.message === 'nothingToUse').length, 2)
})

test('the keybind ignores a failure note', async () => {
  resetCalls()
  view('A')
  serve('A', null, failure('A', 'auth'))
  render()
  keybind().run()
  await sleep(1)
  assert.deepEqual(calls.clip, [])
  assert.ok(note(), 'the note stays')
})

test('palette dismiss clears the visible suggestion', () => {
  resetCalls()
  view('A')
  serve('A', suggestion('A', 'Quítame'))
  render()
  command('next-prompt.dismiss').run()
  assert.equal(pillText(), null)
  assert.deepEqual(ctx.restCalls.at(-1), ['/dismiss?session_id=A', 'POST'])
  assert.deepEqual(calls.clip, [])
})
