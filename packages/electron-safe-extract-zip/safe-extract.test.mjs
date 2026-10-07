import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'
import { afterEach, test } from 'node:test'
import { fileURLToPath } from 'node:url'

const require = createRequire(import.meta.url)
const packageRoot = path.dirname(fileURLToPath(import.meta.url))
const { extract, resolveExtractorCommand, validateZipArchive } = require('./index.cjs')
const roots = []

afterEach(() => {
  for (const root of roots.splice(0)) fs.rmSync(root, { recursive: true, force: true })
})

test('resolves only an absolute trusted OS extractor', () => {
  const exists = (candidate) => candidate === '/usr/bin/unzip'
  const linux = resolveExtractorCommand('linux', {}, exists)
  assert.equal(linux.command, '/usr/bin/unzip')
  assert.deepEqual(linux.args('/archive.zip', '/output'), [
    '-q',
    '/archive.zip',
    '-d',
    '/output'
  ])

  const python = resolveExtractorCommand(
    'linux',
    {},
    (candidate) => candidate === '/usr/bin/python3'
  )
  assert.equal(python.command, '/usr/bin/python3')
  assert.deepEqual(python.args('/archive.zip', '/output'), [
    path.join(packageRoot, 'extract.py'),
    '/archive.zip',
    '/output'
  ])

  const windowsTar = String.raw`C:\Windows\System32\tar.exe`
  const windows = resolveExtractorCommand(
    'win32',
    { SystemRoot: String.raw`C:\Windows` },
    (p) => p === windowsTar
  )
  assert.equal(windows.command, windowsTar)
  assert.deepEqual(windows.args(String.raw`C:\archive.zip`, String.raw`C:\output`), [
    '-xf',
    String.raw`C:\archive.zip`,
    '-C',
    String.raw`C:\output`
  ])

  assert.throws(
    () => resolveExtractorCommand('linux', {}, () => false),
    /trusted ZIP extractor is unavailable/
  )
})

test('extracts the checksummed Electron ZIP without a native addon', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'electron-safe-extract-'))
  roots.push(root)
  const archive = path.join(root, 'electron.zip')
  const output = path.join(root, 'dist')
  fs.writeFileSync(
    archive,
    Buffer.from(
      'UEsDBBQAAAAIABmuRl1eKmA0EQAAAA8AAAAQAAAAbmVzdGVkL2hlbGxvLnR4dMtIzcnJV0jNSU0uKcrP4wIAUEsDBBQAAAAIAAAAIQDihkXDEwAAABEAAAAIAAAAZWxlY3Ryb25TVtRPyszTL87gSq3ILFEw4AIAUEsBAhQDFAAAAAgAGa5GXV4qYDQRAAAADwAAABAAAAAAAAAAAAAAAIABAAAAAG5lc3RlZC9oZWxsby50eHRQSwECFAMUAAAACAAAACEA4oZFwxMAAAARAAAACAAAAAAAAAAAAAAA7YE/AAAAZWxlY3Ryb25QSwUGAAAAAAIAAgB0AAAAeAAAAAAA',
      'base64'
    )
  )

  await extract(archive, { dir: output })

  assert.equal(fs.readFileSync(path.join(output, 'nested', 'hello.txt'), 'utf8'), 'hello electron\n')
  assert.equal(fs.statSync(path.join(output, 'electron')).mode & 0o777, 0o755)
})

test('rejects relative paths before launching an extractor', async () => {
  await assert.rejects(extract('relative.zip', { dir: '/absolute/output' }), /archive path must be absolute/)
  await assert.rejects(extract('/absolute/archive.zip', { dir: 'relative/output' }), /output directory must be absolute/)
})

test('rejects ZIP members that escape the output directory', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'electron-safe-traversal-'))
  roots.push(root)
  const archive = path.join(root, 'traversal.zip')
  const output = path.join(root, 'dist')
  fs.writeFileSync(
    archive,
    Buffer.from(
      'UEsDBBQAAAAAAG2uRl0QP9GrBAAAAAQAAAANAAAALi4vZXNjYXBlLnR4dG5vcGVQSwECFAMUAAAAAABtrkZdED/RqwQAAAAEAAAADQAAAAAAAAAAAAAAgAEAAAAALi4vZXNjYXBlLnR4dFBLBQYAAAAAAQABADsAAAAvAAAAAAA=',
      'base64'
    )
  )

  await assert.rejects(extract(archive, { dir: output }), /unsafe ZIP member/)
  assert.equal(fs.existsSync(path.join(root, 'escape.txt')), false)
})

test('rejects ZIP symlinks that escape the output directory', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'electron-safe-symlink-'))
  roots.push(root)
  const archive = path.join(root, 'symlink.zip')
  const output = path.join(root, 'dist')
  fs.writeFileSync(
    archive,
    Buffer.from(
      'UEsDBBQAAAAIAAAAIQB/Y5L8EgAAABAAAAALAAAAbmVzdGVkL2xpbmvT09PX09NPLU5OLEjVK6koAQBQSwECFAMUAAAACAAAACEAf2OS/BIAAAAQAAAACwAAAAAAAAAAAAAA/6EAAAAAbmVzdGVkL2xpbmtQSwUGAAAAAAEAAQA5AAAAOwAAAAAA',
      'base64'
    )
  )

  await assert.rejects(extract(archive, { dir: output }), /unsafe ZIP symlink target/)
  assert.equal(fs.existsSync(path.join(root, 'escape.txt')), false)
})

test('rejects extractor-specific Unicode path overrides', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'electron-safe-unicode-path-'))
  roots.push(root)
  const archive = path.join(root, 'unicode-path.zip')
  fs.writeFileSync(
    archive,
    Buffer.from(
      'UEsDBBQAAAAIAAAAIQAQP9GrBgAAAAQAAAAIABYAc2FmZS50eHR1cBIAAWtE2EYuLi9lc2NhcGUudHh0y8svSAUAUEsBAhQDFAAAAAgAAAAhABA/0asGAAAABAAAAAgAFgAAAAAAAAAAAIABAAAAAHNhZmUudHh0dXASAAFrRNhGLi4vZXNjYXBlLnR4dFBLBQYAAAAAAQABAEwAAABCAAAAAAA=',
      'base64'
    )
  )

  await assert.rejects(validateZipArchive(archive), /Unicode path overrides are unsupported/)
})

test('rejects Windows drive-relative member and symlink paths', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'electron-safe-drive-relative-'))
  roots.push(root)
  const memberArchive = path.join(root, 'member.zip')
  const symlinkArchive = path.join(root, 'symlink.zip')
  fs.writeFileSync(
    memberArchive,
    Buffer.from(
      'UEsDBBQAAAAIABSMR10QP9GrBgAAAAQAAAAMAAAAQzplc2NhcGUudHh0y8svSAUAUEsBAhQDFAAAAAgAFIxHXRA/0asGAAAABAAAAAwAAAAAAAAAAAAAAIABAAAAAEM6ZXNjYXBlLnR4dFBLBQYAAAAAAQABADoAAAAwAAAAAAA=',
      'base64'
    )
  )
  fs.writeFileSync(
    symlinkArchive,
    Buffer.from(
      'UEsDBBQAAAAIAAAAIQB8y8OCDgAAAAwAAAALAAAAbmVzdGVkL2xpbmtztkotTk4sSNUrqSgBAFBLAQIUAxQAAAAIAAAAIQB8y8OCDgAAAAwAAAALAAAAAAAAAAAAAAD/oQAAAABuZXN0ZWQvbGlua1BLBQYAAAAAAQABADkAAAA3AAAAAAA=',
      'base64'
    )
  )

  await assert.rejects(validateZipArchive(memberArchive), /unsafe ZIP member/)
  await assert.rejects(validateZipArchive(symlinkArchive), /unsafe ZIP symlink target/)
})

test('accepts an EOCD signature inside a valid ZIP comment', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'electron-safe-comment-'))
  roots.push(root)
  const archive = path.join(root, 'comment.zip')
  fs.writeFileSync(
    archive,
    Buffer.from(
      'UEsDBBQAAAAIADGMR11H3dx5BAAAAAIAAAAIAAAAc2FmZS50eHTLzwYAUEsBAhQDFAAAAAgAMYxHXUfd3HkEAAAAAgAAAAgAAAAAAAAAAAAAAIABAAAAAHNhZmUudHh0UEsFBgAAAAABAAEANgAAACoAAAAyAHByZWZpeFBLBQZ4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4',
      'base64'
    )
  )

  await validateZipArchive(archive)
})

test('rejects a symlink destination before extracting', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'electron-safe-destination-'))
  roots.push(root)
  const archive = path.join(root, 'electron.zip')
  const outside = path.join(root, 'outside')
  const output = path.join(root, 'dist')
  fs.mkdirSync(outside)
  fs.symlinkSync(outside, output, 'dir')
  fs.writeFileSync(
    archive,
    Buffer.from(
      'UEsDBBQAAAAIABmuRl1eKmA0EQAAAA8AAAAQAAAAbmVzdGVkL2hlbGxvLnR4dMtIzcnJV0jNSU0uKcrP4wIAUEsDBBQAAAAIAAAAIQDihkXDEwAAABEAAAAIAAAAZWxlY3Ryb25TVtRPyszTL87gSq3ILFEw4AIAUEsBAhQDFAAAAAgAGa5GXV4qYDQRAAAADwAAABAAAAAAAAAAAAAAAIABAAAAAG5lc3RlZC9oZWxsby50eHRQSwECFAMUAAAACAAAACEA4oZFwxMAAAARAAAACAAAAAAAAAAAAAAA7YE/AAAAZWxlY3Ryb25QSwUGAAAAAAIAAgB0AAAAeAAAAAAA',
      'base64'
    )
  )

  await assert.rejects(extract(archive, { dir: output }), /output directory must not be a symlink/)
  assert.deepEqual(fs.readdirSync(outside), [])
})
