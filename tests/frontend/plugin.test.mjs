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
const pill = () => find(render(), n => n.type === 'button')
const pillText = () => {
  const button = pill()
  return button ? textOf(button).trim() : null
}
const closeButton = () => find(render(), n => n.props?.role === 'button')

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
