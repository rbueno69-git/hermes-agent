import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { expect, test } from 'vitest'

const connectorRehearsal = path.resolve('../apps/desktop/scripts/connector-rehearsal.mjs')

function writeExecutable(file: string, source: string) {
  fs.mkdirSync(path.dirname(file), { recursive: true })
  fs.writeFileSync(file, source, { mode: 0o755 })
}

test('connector rehearsal launches a hoisted Electron CLI with an unsplit entry argument', () => {
  const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes electron ;$(path-confusion) '))
  const desktop = path.join(fixture, 'apps', 'desktop')
  const scripts = path.join(desktop, 'scripts')
  const invocation = path.join(fixture, 'electron-invocation.json')
  const shellSentinel = path.join(fixture, 'path-confusion')
  fs.mkdirSync(path.join(desktop, 'dist'), { recursive: true })
  fs.mkdirSync(scripts, { recursive: true })
  fs.symlinkSync(connectorRehearsal, path.join(scripts, 'connector-rehearsal.mjs'))
  fs.symlinkSync(path.resolve('../apps/desktop/scripts/resolve-electron-command.mjs'), path.join(scripts, 'resolve-electron-command.mjs'))
  fs.writeFileSync(path.join(desktop, 'dist', 'electron-main.mjs'), '')

  writeExecutable(
    path.join(fixture, 'node_modules', '.bin', 'vite'),
    `#!/usr/bin/env node\nconst http = require('node:http')\nconst server = http.createServer((_req, res) => res.end('ok')).listen(5194, '127.0.0.1')\nprocess.on('SIGTERM', () => server.close())\n`,
  )
  fs.mkdirSync(path.join(fixture, 'node_modules', 'electron'), { recursive: true })
  fs.writeFileSync(
    path.join(fixture, 'node_modules', 'electron', 'package.json'),
    JSON.stringify({ name: 'electron', version: '42.11.3', main: 'index.js' }),
  )
  writeExecutable(
    path.join(fixture, 'node_modules', 'electron', 'cli.js'),
    `#!/usr/bin/env node\nrequire('node:fs').writeFileSync(${JSON.stringify(invocation)}, JSON.stringify(process.argv.slice(2)))\n`,
  )

  try {
    const result = spawnSync(
      process.execPath,
      ['--preserve-symlinks-main', path.join(scripts, 'connector-rehearsal.mjs'), path.join(fixture, 'sandbox ; touch nope')],
      {
        cwd: fixture,
        encoding: 'utf8',
        env: {
          ...process.env,
          HERMES_DESKTOP_PYTHON: process.execPath,
        },
        timeout: 15_000,
      },
    )

    expect(result.status, result.stderr).toBe(0)
    expect(JSON.parse(fs.readFileSync(invocation, 'utf8'))).toEqual([path.join(desktop, 'dist', 'connector-rehearsal.mjs')])
    expect(fs.existsSync(shellSentinel)).toBe(false)
  } finally {
    fs.rmSync(fixture, { recursive: true, force: true })
  }
})

test('Electron 42 CLI acquires its binary when the package has no dist', () => {
  const fixture = fs.mkdtempSync(path.resolve('../node_modules/.hermes-electron-download-'))
  const electronPackage = path.resolve('../node_modules/electron')
  const fixturePackage = path.join(fixture, 'electron')
  const cache = path.join(fixture, 'cache')
  fs.cpSync(electronPackage, fixturePackage, {
    recursive: true,
    filter: source => !['dist', 'path.txt'].includes(path.basename(source)),
  })

  try {
    const result = spawnSync(process.execPath, [path.join(fixturePackage, 'cli.js'), '--no-sandbox', '--version'], {
      encoding: 'utf8',
      env: { ...process.env, ELECTRON_CACHE: cache },
      timeout: 300_000,
    })

    expect(result.status, result.stderr).toBe(0)
    expect(result.stdout).toContain('Downloading Electron binary...')
    expect(result.stdout.trim().endsWith('v42.11.3')).toBe(true)
    expect(fs.existsSync(path.join(fixturePackage, 'dist'))).toBe(true)
  } finally {
    fs.rmSync(fixture, { recursive: true, force: true })
  }
}, 310_000)
