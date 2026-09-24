// Loads desktop/plugin.js against the stub SDK and renders its contributions
// into plain objects. The plugin file ships uncompiled, so the only rewrite
// needed is pointing its three import specifiers at the stubs.

import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'

import { calls, host, server } from './stubs/sdk.mjs'

const HERE = new URL('.', import.meta.url)
export const PLUGIN_SOURCE = readFileSync(new URL('../../desktop/plugin.js', HERE), 'utf8')

export async function loadPlugin() {
  const patched = PLUGIN_SOURCE.replace("from '@hermes/plugin-sdk'", "from '../stubs/sdk.mjs'")
    .replace("from 'react/jsx-runtime'", "from '../stubs/jsx.mjs'")
    .replace("from 'react'", "from '../stubs/react.mjs'")
  const left = [...patched.matchAll(/^import .* from '([^']+)'/gm)].map(m => m[1]).filter(s => !s.startsWith('../stubs/'))
  if (left.length) throw new Error(`unexpected imports in plugin.js: ${left.join(', ')}`)
  mkdirSync(new URL('.build/', HERE), { recursive: true })
  const out = new URL('.build/plugin.mjs', HERE)
  writeFileSync(out, patched)
  return (await import(out.href)).default
}

export function makeCtx() {
  const ctx = {
    contributions: [],
    disposers: [],
    restCalls: [],
    restResult: {},
    i18n: { register() {}, t: key => key },
    register: c => {
      ctx.contributions.push(c)
      return () => {}
    },
    registerMany: list => list.map(c => ctx.register(c)),
    onDispose: fn => ctx.disposers.push(fn),
    rest: async (path, opts) => {
      ctx.restCalls.push([path, opts?.method || 'GET'])
      return ctx.restResult
    },
    os: {
      writeClipboard: async text => {
        calls.clip.push(text)
        return true
      }
    }
  }
  return ctx
}

export const contribution = (ctx, area) => ctx.contributions.find(c => c.area === area)

/** Expand function components into a plain element tree. */
export function expand(node) {
  if (node == null || typeof node !== 'object') return node
  if (Array.isArray(node)) return node.map(expand)
  if (typeof node.type === 'function') return expand(node.type(node.props || {}))
  const children = node.props?.children
  return { type: node.type, props: { ...node.props, children: expand(children) } }
}

export function find(node, pred) {
  if (node == null || typeof node !== 'object') return null
  if (Array.isArray(node)) {
    for (const n of node) {
      const hit = find(n, pred)
      if (hit) return hit
    }
    return null
  }
  if (pred(node)) return node
  return find(node.props?.children, pred)
}

export const textOf = node => {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node !== 'object') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  return textOf(node.props?.children)
}

export function resetCalls() {
  for (const key of Object.keys(calls)) calls[key].length = 0
}

/** Put the app in a state: which chat is focused and whether it is working. */
export function view(storedId, busy = false) {
  host.state.focusedStoredSessionId.set(storedId)
  host.state.busy.set(busy)
}

/** The backend answered GET /suggestion for this session just now. */
export function serve(storedId, suggestion) {
  server.set(storedId, { data: suggestion, dataUpdatedAt: Date.now() + 1, error: null })
}

export const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))
