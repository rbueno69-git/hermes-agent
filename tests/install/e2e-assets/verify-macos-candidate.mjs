#!/usr/bin/env node
import { closeSync, constants, chmodSync, lstatSync, mkdirSync, openSync, readFileSync, readdirSync, writeFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
import path from 'node:path'

function fail(message) {
  console.error(`candidate verification failed: ${message}`)
  process.exit(1)
}

function argsOf(argv) {
  const args = {}
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index]
    const value = argv[index + 1]
    if (!key?.startsWith('--') || value === undefined) fail('arguments must be --name value pairs')
    args[key.slice(2)] = value
  }
  return args
}

function exactKeys(value, expected, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail(`${label} must be an object`)
  const actual = Object.keys(value).sort()
  const wanted = [...expected].sort()
  if (JSON.stringify(actual) !== JSON.stringify(wanted)) fail(`${label} keys must be exactly ${wanted.join(', ')}`)
}

function nonempty(value, label) {
  if (typeof value !== 'string' || value.length === 0 || value.length > 512) fail(`${label} must be a non-empty bounded string`)
}

function digestObject(value, label) {
  exactKeys(value, ['algorithm', 'value'], label)
  if (value.algorithm !== 'sha256' || !/^[0-9a-f]{64}$/.test(value.value)) fail(`${label} must contain a lowercase SHA-256 digest`)
}

function readPinned(file) {
  const noFollow = constants.O_NOFOLLOW ?? 0
  const fd = openSync(file, constants.O_RDONLY | noFollow)
  try {
    return readFileSync(fd)
  } finally {
    closeSync(fd)
  }
}

const args = argsOf(process.argv.slice(2))
for (const name of ['root', 'expected-repository', 'expected-sha', 'expected-package-lock-sha256', 'expected-cargo-lock-sha256', 'expected-node-version', 'expected-rust-toolchain', 'copy-to']) {
  if (!args[name]) fail(`missing --${name}`)
}
if (!/^[0-9a-f]{40}$/.test(args['expected-sha'])) fail('expected SHA must be a full lowercase Git SHA-1')

const root = path.resolve(args.root)
const entries = readdirSync(root).sort()
if (JSON.stringify(entries) !== JSON.stringify(['Hermes-Setup.dmg', 'provenance.json'])) {
  fail(`artifact entries must be exactly Hermes-Setup.dmg and provenance.json; got ${entries.join(', ')}`)
}
for (const name of entries) {
  const stats = lstatSync(path.join(root, name), { throwIfNoEntry: true })
  if (!stats.isFile()) fail(`${name} must be a regular file`)
}

const provenancePath = path.join(root, 'provenance.json')
let provenance
try {
  provenance = JSON.parse(readPinned(provenancePath).toString('utf8'))
} catch (error) {
  fail(`invalid provenance JSON: ${error.message}`)
}
exactKeys(provenance, ['schema', 'repository', 'source_sha', 'arch', 'install_pin', 'signing', 'digest', 'materials'], 'provenance')
if (provenance.schema !== 2) fail('schema must be 2')
if (provenance.repository !== args['expected-repository']) fail('repository does not match caller')
if (provenance.source_sha !== args['expected-sha']) fail('source_sha does not match caller')
if (provenance.arch !== 'arm64' || provenance.signing !== 'adhoc') fail('candidate must be arm64 and ad-hoc signed')
exactKeys(provenance.install_pin, ['branch', 'commit'], 'install_pin')
if (provenance.install_pin.branch !== 'main' || provenance.install_pin.commit !== null) fail('install_pin must follow main without a commit pin')
digestObject(provenance.digest, 'digest')

const materials = provenance.materials
exactKeys(materials, ['node', 'npm', 'rustc', 'cargo', 'xcode', 'package_lock', 'cargo_lock'], 'materials')
for (const tool of ['node', 'npm', 'cargo']) {
  exactKeys(materials[tool], ['version'], `materials.${tool}`)
  nonempty(materials[tool].version, `materials.${tool}.version`)
}
exactKeys(materials.rustc, ['version', 'toolchain'], 'materials.rustc')
nonempty(materials.rustc.version, 'materials.rustc.version')
nonempty(materials.rustc.toolchain, 'materials.rustc.toolchain')
exactKeys(materials.xcode, ['developer_dir', 'version', 'build'], 'materials.xcode')
for (const field of ['developer_dir', 'version', 'build']) nonempty(materials.xcode[field], `materials.xcode.${field}`)
if (!path.isAbsolute(materials.xcode.developer_dir)) fail('Xcode developer_dir must be absolute')
digestObject(materials.package_lock, 'materials.package_lock')
digestObject(materials.cargo_lock, 'materials.cargo_lock')
if (materials.package_lock.value !== args['expected-package-lock-sha256']) fail('package-lock digest does not match exact checkout')
if (materials.cargo_lock.value !== args['expected-cargo-lock-sha256']) fail('Cargo.lock digest does not match exact checkout')
if (materials.node.version !== args['expected-node-version']) fail('Node version does not match the builder pin')
if (materials.rustc.toolchain !== args['expected-rust-toolchain']) fail('Rust toolchain does not match the builder pin')
if (!materials.rustc.version.startsWith(`rustc ${args['expected-rust-toolchain']} `)) fail('rustc version does not match its toolchain')
if (!materials.cargo.version.startsWith(`cargo ${args['expected-rust-toolchain']} `)) fail('Cargo version does not match the Rust toolchain')
if (!/^\d+\.\d+\.\d+/.test(materials.npm.version)) fail('npm version must begin with a semantic version')

const sourceDmg = path.join(root, 'Hermes-Setup.dmg')
const sourceBytes = readPinned(sourceDmg)
const sourceDigest = createHash('sha256').update(sourceBytes).digest('hex')
if (sourceDigest !== provenance.digest.value) fail(`DMG digest mismatch: expected ${provenance.digest.value}, got ${sourceDigest}`)

const copyDir = path.resolve(args['copy-to'])
mkdirSync(copyDir, { mode: 0o700 })
const copyDmg = path.join(copyDir, 'Hermes-Setup.dmg')
writeFileSync(copyDmg, sourceBytes, { flag: 'wx', mode: 0o400 })
const copyDigest = createHash('sha256').update(readPinned(copyDmg)).digest('hex')
if (copyDigest !== sourceDigest) fail('verified copy digest changed while copying')
chmodSync(copyDmg, 0o400)
chmodSync(copyDir, 0o500)

console.log(JSON.stringify({ digest: copyDigest, dmg: copyDmg, provenance: provenancePath }))
