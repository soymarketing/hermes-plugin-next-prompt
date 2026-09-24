// Every name desktop/plugin.js imports from '@hermes/plugin-sdk' must be a real
// export of the Desktop SDK: the runtime shim destructures real exports, so one
// wrong name fails the whole bundle at load time. The stub SDK can't catch that.
//
// Usage: node tests/check_sdk_exports.mjs <hermes-agent checkout>

import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const checkout = process.argv[2]
if (!checkout) {
  console.error('usage: node tests/check_sdk_exports.mjs <hermes-agent checkout>')
  process.exit(2)
}

const plugin = readFileSync(new URL('../desktop/plugin.js', import.meta.url), 'utf8')
const sdk = readFileSync(join(checkout, 'apps/desktop/src/sdk/index.ts'), 'utf8')

const importBlock = plugin.match(/import\s*\{([^}]*)\}\s*from\s*'@hermes\/plugin-sdk'/)
if (!importBlock) {
  console.error('plugin.js has no named import from @hermes/plugin-sdk')
  process.exit(1)
}
const imported = importBlock[1].split(',').map(s => s.trim().split(/\s+as\s+/)[0]).filter(Boolean)

const exported = new Set()
for (const m of sdk.matchAll(/export\s+(?:declare\s+)?(?:const|let|var|function\*?|async\s+function|class)\s+([A-Za-z_$][\w$]*)/g)) {
  exported.add(m[1])
}
for (const m of sdk.matchAll(/export\s*\{([^}]*)\}/g)) {
  for (const part of m[1].split(',')) {
    const name = part.trim().replace(/^type\s+/, '')
    if (!name || part.trim().startsWith('type ')) continue
    const alias = name.split(/\s+as\s+/)
    exported.add((alias[1] || alias[0]).trim())
  }
}

const missing = imported.filter(name => !exported.has(name))
if (missing.length) {
  console.error(`plugin.js imports names the SDK does not export: ${missing.join(', ')}`)
  process.exit(1)
}
console.log(`SDK exports OK (${imported.length} names): ${imported.join(', ')}`)
