import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { expect, test } from 'vitest'

const safeguard = path.resolve('../apps/desktop/scripts/patch-electron-builder-mac-binary.mjs')
const electronMac = path.resolve('../node_modules/app-builder-lib/out/electron/electronMac.js')

test('macOS Electron binary safeguard recognizes the installed builder before packaging', () => {
  const result = spawnSync(process.execPath, [safeguard, '--check', electronMac], { encoding: 'utf8' })

  expect(result.status, result.stderr).toBe(0)
  expect(result.stdout).toContain('[patch-electron-builder] safeguard compatible')
})

test('macOS Electron binary safeguard applies to app-builder-lib 26.16.1 without platform spoofing', () => {
  const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-builder-apply-'))
  const target = path.join(fixture, 'electronMac.js')
  fs.copyFileSync(electronMac, target)

  try {
    const applied = spawnSync(process.execPath, [safeguard, '--apply', target], { encoding: 'utf8' })
    expect(applied.status, applied.stderr).toBe(0)
    expect(applied.stdout).toContain('[patch-electron-builder] applied macOS Electron binary fallback')

    const checked = spawnSync(process.execPath, [safeguard, '--check', target], { encoding: 'utf8' })
    expect(checked.status, checked.stderr).toBe(0)
    const syntax = spawnSync(process.execPath, ['--check', target], { encoding: 'utf8' })
    expect(syntax.status, syntax.stderr).toBe(0)
  } finally {
    fs.rmSync(fixture, { recursive: true, force: true })
  }
})

test('macOS Electron binary safeguard fails closed on an unknown builder shape', () => {
  const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-builder-shape-'))
  const electronMac = path.join(fixture, 'electronMac.js')
  fs.writeFileSync(electronMac, '// hermes-macos-electron-binary-fallback\nmodule.exports = {}\n')

  try {
    const result = spawnSync(process.execPath, [safeguard, '--check', electronMac], { encoding: 'utf8' })
    expect(result.status).not.toBe(0)
    expect(result.stderr).toContain('incompatible app-builder-lib electronMac.js shape')
  } finally {
    fs.rmSync(fixture, { recursive: true, force: true })
  }
})
