import { execFileSync } from 'node:child_process'
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { load } from 'js-yaml'
import { expect, test } from 'vitest'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const generator = path.join(root, 'scripts/sandbox/generate-e2e-matrix.mjs')
const tags = JSON.stringify([{ ref: 'v2026.6.19', desktop: true }])
const workflow = relative => load(readFileSync(path.join(root, relative), 'utf8'))
const verifier = path.join(root, 'tests/install/e2e-assets/verify-macos-candidate.mjs')
const digestVerifier = path.join(root, 'tests/install/e2e-assets/verify-sha256.sh')

function generate(route = 'all') {
  return JSON.parse(execFileSync(process.execPath, [generator, '--tags', tags, '--route', route], {
    encoding: 'utf8',
  }))
}

function plan() {
  return execFileSync(process.execPath, [generator, '--tags', tags, '--format', 'markdown'], {
    encoding: 'utf8',
  })
}

test('the macOS candidate replaces latest and covers historical starts plus HEAD to NEXT', () => {
  const matrices = generate()
  const candidate = matrices.macos.include.filter(leg => leg.install_method === 'desktop-installer@candidate')

  expect(candidate.length).toBeGreaterThan(0)
  const expectedStarts = new Set(['v2026.6.19 -> HEAD', 'HEAD -> NEXT'])
  expect(new Set(candidate.map(leg => `${leg.install_ref} -> ${leg.update_ref}`))).toEqual(expectedStarts)
  expect(candidate.every(leg => expectedStarts.has(`${leg.install_ref} -> ${leg.update_ref}`))).toBe(true)
  expect(candidate.every(leg => leg.tag_has_desktop === true)).toBe(true)
  expect(candidate.every(leg => leg.name.includes('desktop-installer@candidate'))).toBe(true)
  expect(matrices.macos.include.some(leg => leg.install_method === 'desktop-installer@latest')).toBe(false)
  expect(matrices.windows.include.some(leg => leg.install_method === 'desktop-installer@latest')).toBe(true)
  expect(matrices.windows.include.some(leg => leg.install_method.includes('@candidate'))).toBe(false)
})

test('the plan counts candidate historical starts as runnable', () => {
  const matrices = generate()
  const total = Object.values(matrices).reduce((count, matrix) => count + matrix.include.length, 0)
  const markdown = plan()

  expect(markdown).toContain(`= ${total} legs`)
  expect(markdown).toMatch(/macos: desktop-installer@candidate -> hermes-update.*⏳.*⏳/)
  expect(markdown).not.toContain('current-SHA only')
})

test('the caller builds one exact-SHA ad-hoc macOS artifact and fails closed when candidate build fails', () => {
  const caller = workflow('.github/workflows/install-e2e.yml')
  const builder = caller.jobs['macos-candidate-installer']
  const matrix = caller.jobs['generate-matrix']
  const macos = caller.jobs.macos

  expect(matrix.outputs['macos-candidate-legs']).toContain('steps.gen.outputs.macos-candidate-legs')
  expect(builder['runs-on']).toBe('macos-latest')
  expect(builder.if).toContain("needs.generate-matrix.outputs.macos-candidate-legs != '0'")
  expect(builder.steps[0]).toMatchObject({ with: { ref: '${{ github.sha }}' } })

  const preflight = builder.steps.find(step => step.name === 'Verify exact source checkout')
  const postflight = builder.steps.find(step => step.name === 'Verify source checkout remained clean')
  expect(preflight.env.EXPECTED_SHA).toBe('${{ github.sha }}')
  expect(preflight.run).toContain('git diff --quiet')
  expect(preflight.run).toContain('git diff --cached --quiet')
  expect(postflight.run).toContain('git diff --quiet')
  expect(postflight.run).toContain('git diff --cached --quiet')

  const xcode = builder.steps.find(step => step.name === 'Select and record the installed Xcode')
  expect(xcode.run).toContain('xcode-select -p')
  expect(xcode.run).toContain('xcodebuild -version')
  expect(xcode.run).not.toContain('Xcode_26')

  const build = builder.steps.find(step => step.name === 'Build ad-hoc candidate installer')
  expect(build.env).toMatchObject({
    HERMES_BUILD_PIN_BRANCH: 'main',
    HERMES_BUILD_PIN_COMMIT: '',
    RUSTUP_TOOLCHAIN: '1.91.1',
  })
  expect(build.run).toContain('rustc --version')
  expect(build.run).toContain('cargo --version')
  expect(build.run).toContain('--bundles app')
  expect(build.run).toContain('build --locked')
  expect(build.run).toContain('-- --locked')
  expect(build.run).toContain('codesign --force --deep --sign -')
  expect(build.run).toContain('hdiutil create')
  expect(build.run).not.toContain('dmg-volume.icns')

  const provenance = builder.steps.find(step => step.name === 'Write candidate provenance')
  for (const field of ['repository', 'source_sha', 'arch', 'install_pin', 'signing', 'digest', 'materials', 'package_lock', 'cargo_lock', 'xcode']) {
    expect(provenance.run).toContain(field)
  }
  expect(provenance.run).toContain('install_pin: {branch: "main", commit: null}')

  expect(builder.steps.some(step => step.uses === 'actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a')).toBe(true)
  expect(macos.needs).toEqual(['generate-matrix', 'macos-candidate-installer'])
  expect(macos.if).toContain("needs.macos-candidate-installer.result == 'success'")
  expect(macos.if).toContain("needs.generate-matrix.outputs.macos-candidate-legs == '0'")
})

test('large install evidence is uploaded only for failed or cancelled legs and expires after one day', () => {
  const contracts = [
    ['.github/workflows/install-e2e-run.yml', ['Upload installer logs']],
    ['.github/workflows/install-e2e-macos-run.yml', ['Upload logs', 'Upload logs']],
    ['.github/workflows/install-e2e-windows-run.yml', ['Upload native acceptance evidence', 'Upload proof + logs']],
  ]

  for (const [relative, names] of contracts) {
    const parsed = workflow(relative)
    const uploadSteps = Object.values(parsed.jobs).flatMap(job => job.steps ?? [])
      .filter(step => names.includes(step.name))
    expect(uploadSteps.map(step => step.name)).toEqual(expect.arrayContaining(names))
    for (const step of uploadSteps) {
      expect(step.if).toContain('failure()')
      expect(step.if).toContain('cancelled()')
      expect(step.with['retention-days']).toBe(1)
    }
  }

  const caller = workflow('.github/workflows/install-e2e.yml')
  const player = caller.jobs['leg-player'].steps.find(step => step.uses?.startsWith('actions/upload-artifact@'))
  expect(player.with['retention-days']).toBe(1)

  const windows = workflow('.github/workflows/install-e2e-windows-run.yml')
  const known = windows.jobs.e2e.steps.find(step => step.name === 'Upload known-failure receipt')
  expect(known.with['retention-days']).toBe(1)
})

test('candidate legs consume and verify only the same-run exact provenance before using a file URL', () => {
  const reusable = workflow('.github/workflows/install-e2e-macos-run.yml')
  const gui = reusable.jobs['gui-e2e']

  expect(reusable.on.workflow_call.inputs['dmg-url'].default).toBe('')
  expect(gui.if).toContain('desktop-installer@candidate')
  const download = gui.steps.find(step => step.uses === 'actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c')
  expect(download.if).toContain('desktop-installer@candidate')
  expect(download.with).not.toHaveProperty('run-id')
  expect(download.with).not.toHaveProperty('repository')

  const verify = gui.steps.find(step => step.name === 'Verify candidate installer provenance and payload')
  expect(verify.env).toMatchObject({
    EXPECTED_REPOSITORY: '${{ github.repository }}',
    EXPECTED_SHA: '${{ github.sha }}',
  })
  for (const witness of ['verify-macos-candidate.mjs', 'codesign --verify', 'arm64', 'Applications', 'file://']) {
    expect(verify.run).toContain(witness)
  }
  expect(verify.run).toContain('chflags uchg')
  const install = gui.steps.find(step => step.name?.startsWith('Install ${{ inputs.install-ref }}'))
  expect(install.env.DMG_URL).toContain('steps.candidate.outputs.dmg-url')
  expect(install.env.DMG_SHA256).toContain('steps.candidate.outputs.digest')
  expect(install.run).toContain("--install-method '${{ inputs.install-method }}'")
  expect(install.run).toContain('--dmg-sha256 "$DMG_SHA256"')

  const setupIndex = gui.steps.findIndex(step => step.name === 'Set up driver tools only')
  const npmIndex = gui.steps.findIndex(step => step.name === 'Install locked chat driver dependencies')
  const verifyIndex = gui.steps.findIndex(step => step.name === 'Verify candidate installer provenance and payload')
  const stageIndex = gui.steps.findIndex(step => step.name?.startsWith('Stage serve repo'))
  expect(verifyIndex).toBeGreaterThan(setupIndex)
  expect(verifyIndex).toBeGreaterThan(npmIndex)
  expect(verifyIndex).toBe(stageIndex - 1)
})

function candidateFixture() {
  const base = mkdtempSync(path.join(os.tmpdir(), 'hermes-candidate-'))
  const artifact = path.join(base, 'artifact')
  const verified = path.join(base, 'verified')
  mkdirSync(artifact)
  const dmg = path.join(artifact, 'Hermes-Setup.dmg')
  writeFileSync(dmg, 'candidate bytes')
  const digest = execFileSync('sha256sum', [dmg], { encoding: 'utf8' }).split(' ')[0]
  writeFileSync(path.join(artifact, 'provenance.json'), JSON.stringify({
    schema: 2,
    repository: 'NousResearch/Hermes-Agent',
    source_sha: 'a'.repeat(40),
    arch: 'arm64',
    install_pin: { branch: 'main', commit: null },
    signing: 'adhoc',
    digest: { algorithm: 'sha256', value: digest },
    materials: {
      node: { version: 'v24.11.1' },
      npm: { version: '11.9.0' },
      rustc: { version: 'rustc 1.91.1 (ed61e7d7e 2025-11-07)', toolchain: '1.91.1' },
      cargo: { version: 'cargo 1.91.1 (ea2d97820 2025-10-10)' },
      xcode: { developer_dir: '/Applications/Xcode.fixture/Contents/Developer', version: 'fixture-version', build: 'fixture-build' },
      package_lock: { algorithm: 'sha256', value: 'b'.repeat(64) },
      cargo_lock: { algorithm: 'sha256', value: 'c'.repeat(64) },
    },
  }))
  return { artifact, base, digest, dmg, verified }
}

test('portable candidate verifier creates a private read-only copy and rejects extras or tampering', () => {
  const fixture = candidateFixture()
  const output = JSON.parse(execFileSync(process.execPath, [verifier,
    '--root', fixture.artifact,
    '--expected-repository', 'NousResearch/Hermes-Agent',
    '--expected-sha', 'a'.repeat(40),
    '--expected-package-lock-sha256', 'b'.repeat(64),
    '--expected-cargo-lock-sha256', 'c'.repeat(64),
    '--expected-node-version', 'v24.11.1',
    '--expected-rust-toolchain', '1.91.1',
    '--copy-to', fixture.verified,
  ], { encoding: 'utf8' }))

  expect(output.digest).toBe(fixture.digest)
  expect(statSync(output.dmg).mode & 0o777).toBe(0o400)
  expect(statSync(fixture.verified).mode & 0o777).toBe(0o500)

  expect(() => execFileSync(process.execPath, [verifier,
    '--root', fixture.artifact,
    '--expected-repository', 'NousResearch/Hermes-Agent',
    '--expected-sha', 'a'.repeat(40),
    '--expected-package-lock-sha256', 'd'.repeat(64),
    '--expected-cargo-lock-sha256', 'c'.repeat(64),
    '--expected-node-version', 'v24.11.1',
    '--expected-rust-toolchain', '1.91.1',
    '--copy-to', path.join(fixture.base, 'wrong-material-copy'),
  ])).toThrow()

  writeFileSync(path.join(fixture.artifact, 'extra.txt'), 'not allowed')
  expect(() => execFileSync(process.execPath, [verifier,
    '--root', fixture.artifact,
    '--expected-repository', 'NousResearch/Hermes-Agent',
    '--expected-sha', 'a'.repeat(40),
    '--expected-package-lock-sha256', 'b'.repeat(64),
    '--expected-cargo-lock-sha256', 'c'.repeat(64),
    '--expected-node-version', 'v24.11.1',
    '--expected-rust-toolchain', '1.91.1',
    '--copy-to', path.join(fixture.base, 'second-copy'),
  ])).toThrow()

  const tampered = candidateFixture()
  writeFileSync(tampered.dmg, 'changed after provenance')
  expect(() => execFileSync(process.execPath, [verifier,
    '--root', tampered.artifact,
    '--expected-repository', 'NousResearch/Hermes-Agent',
    '--expected-sha', 'a'.repeat(40),
    '--expected-package-lock-sha256', 'b'.repeat(64),
    '--expected-cargo-lock-sha256', 'c'.repeat(64),
    '--expected-node-version', 'v24.11.1',
    '--expected-rust-toolchain', '1.91.1',
    '--copy-to', tampered.verified,
  ])).toThrow()

  const malformed = candidateFixture()
  const provenancePath = path.join(malformed.artifact, 'provenance.json')
  const provenance = JSON.parse(readFileSync(provenancePath, 'utf8'))
  delete provenance.materials.xcode.build
  writeFileSync(provenancePath, JSON.stringify(provenance))
  expect(() => execFileSync(process.execPath, [verifier,
    '--root', malformed.artifact,
    '--expected-repository', 'NousResearch/Hermes-Agent',
    '--expected-sha', 'a'.repeat(40),
    '--expected-package-lock-sha256', 'b'.repeat(64),
    '--expected-cargo-lock-sha256', 'c'.repeat(64),
    '--expected-node-version', 'v24.11.1',
    '--expected-rust-toolchain', '1.91.1',
    '--copy-to', malformed.verified,
  ])).toThrow()
})

test('the driver digest verifier fails closed when local candidate bytes change', () => {
  const fixture = candidateFixture()
  execFileSync('bash', [digestVerifier, fixture.dmg, fixture.digest])
  chmodSync(fixture.dmg, 0o600)
  writeFileSync(fixture.dmg, 'mutated local bytes')
  expect(() => execFileSync('bash', [digestVerifier, fixture.dmg, fixture.digest])).toThrow()
})
