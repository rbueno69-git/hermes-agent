'use strict'

const fs = require('node:fs')
const path = require('node:path')
const { spawn } = require('node:child_process')
const { inflateRawSync } = require('node:zlib')

const STDERR_LIMIT = 16 * 1024
const PYTHON_EXTRACTOR = path.join(__dirname, 'extract.py')
const EOCD_SIGNATURE = 0x06054b50
const CENTRAL_SIGNATURE = 0x02014b50
const LOCAL_SIGNATURE = 0x04034b50
const UNIX_FILE_TYPE = 0o170000
const UNIX_SYMLINK = 0o120000

function unsafeMember(name) {
  throw new Error(`unsafe ZIP member: ${JSON.stringify(name)}`)
}

function canonicalMemberPath(name) {
  if (!name || name.includes('\0')) unsafeMember(name)
  const portable = name.replaceAll('\\', '/')
  if (portable.startsWith('/') || /^[A-Za-z]:/.test(portable)) {
    unsafeMember(name)
  }
  const parts = portable.split('/').filter((part) => part !== '' && part !== '.')
  if (
    parts.length === 0 ||
    parts.includes('..') ||
    parts.some((part) => part.includes(':') || part.replace(/[ .]+$/u, '') === '..')
  ) {
    unsafeMember(name)
  }
  return parts.join('/')
}

function symlinkTarget(member, rawTarget) {
  if (!rawTarget || rawTarget.includes('\0')) {
    throw new Error(`unsafe ZIP symlink target: ${JSON.stringify(member)}`)
  }
  const portable = rawTarget.replaceAll('\\', '/')
  if (portable.startsWith('/') || /^[A-Za-z]:/.test(portable) || portable.split('/').some((part) => part.includes(':'))) {
    throw new Error(`unsafe ZIP symlink target: ${JSON.stringify(member)}`)
  }
  const resolved = path.posix.normalize(path.posix.join(path.posix.dirname(member), portable))
  if (
    resolved === '..' ||
    resolved.startsWith('../') ||
    resolved.split('/').some((part) => part.replace(/[ .]+$/u, '') === '..')
  ) {
    throw new Error(`unsafe ZIP symlink target: ${JSON.stringify(member)}`)
  }
  return resolved
}

function rejectUnsafeExtraFields(extra) {
  let offset = 0
  while (offset < extra.length) {
    if (offset + 4 > extra.length) throw new Error('invalid ZIP extra field')
    const id = extra.readUInt16LE(offset)
    const size = extra.readUInt16LE(offset + 2)
    offset += 4
    if (offset + size > extra.length) throw new Error('invalid ZIP extra field length')
    if (id === 0x7075) throw new Error('ZIP Unicode path overrides are unsupported')
    offset += size
  }
}

function findEndOfCentralDirectory(archive) {
  const floor = Math.max(0, archive.length - 65_557)
  for (let offset = archive.length - 22; offset >= floor; offset -= 1) {
    if (
      archive.readUInt32LE(offset) === EOCD_SIGNATURE &&
      offset + 22 + archive.readUInt16LE(offset + 20) === archive.length
    ) {
      return offset
    }
  }
  throw new Error('invalid ZIP: end of central directory is missing')
}

function readSymlinkTarget(archive, entry) {
  const { compressedSize, compression, localOffset, member, uncompressedSize } = entry
  if (uncompressedSize > 4096 || compressedSize > 65_536) {
    throw new Error(`unsafe ZIP symlink target: ${JSON.stringify(member)}`)
  }
  if (localOffset < 0 || localOffset + 30 > archive.length || archive.readUInt32LE(localOffset) !== LOCAL_SIGNATURE) {
    throw new Error('invalid ZIP local header')
  }
  const nameLength = archive.readUInt16LE(localOffset + 26)
  const extraLength = archive.readUInt16LE(localOffset + 28)
  const start = localOffset + 30 + nameLength + extraLength
  const end = start + compressedSize
  if (end > archive.length) throw new Error('invalid ZIP symlink payload')
  const payload = archive.subarray(start, end)
  const raw = compression === 0 ? payload : compression === 8 ? inflateRawSync(payload) : null
  if (raw === null || raw.length !== uncompressedSize) {
    throw new Error(`unsupported ZIP symlink encoding: ${JSON.stringify(member)}`)
  }
  return raw.toString('utf8')
}

async function validateZipArchive(archivePath) {
  const archive = await fs.promises.readFile(archivePath)
  const eocd = findEndOfCentralDirectory(archive)
  const disk = archive.readUInt16LE(eocd + 4)
  const centralDisk = archive.readUInt16LE(eocd + 6)
  const diskEntries = archive.readUInt16LE(eocd + 8)
  const entries = archive.readUInt16LE(eocd + 10)
  const centralSize = archive.readUInt32LE(eocd + 12)
  let offset = archive.readUInt32LE(eocd + 16)
  if (
    disk !== 0 ||
    centralDisk !== 0 ||
    diskEntries !== entries ||
    entries === 0xffff ||
    centralSize === 0xffffffff ||
    offset === 0xffffffff
  ) {
    throw new Error('unsupported ZIP64 or multi-disk archive')
  }
  const centralEnd = offset + centralSize
  if (centralEnd > eocd || centralEnd > archive.length) throw new Error('invalid ZIP central directory')

  for (let index = 0; index < entries; index += 1) {
    if (offset + 46 > centralEnd || archive.readUInt32LE(offset) !== CENTRAL_SIGNATURE) {
      throw new Error('invalid ZIP central directory entry')
    }
    const flags = archive.readUInt16LE(offset + 8)
    const compression = archive.readUInt16LE(offset + 10)
    const compressedSize = archive.readUInt32LE(offset + 20)
    const uncompressedSize = archive.readUInt32LE(offset + 24)
    const nameLength = archive.readUInt16LE(offset + 28)
    const extraLength = archive.readUInt16LE(offset + 30)
    const commentLength = archive.readUInt16LE(offset + 32)
    const externalAttributes = archive.readUInt32LE(offset + 38)
    const localOffset = archive.readUInt32LE(offset + 42)
    const entryEnd = offset + 46 + nameLength + extraLength + commentLength
    if (entryEnd > centralEnd) throw new Error('invalid ZIP central directory entry length')
    const member = canonicalMemberPath(archive.subarray(offset + 46, offset + 46 + nameLength).toString('utf8'))
    rejectUnsafeExtraFields(archive.subarray(offset + 46 + nameLength, offset + 46 + nameLength + extraLength))
    if ((flags & 1) !== 0) throw new Error(`encrypted ZIP member is unsupported: ${JSON.stringify(member)}`)
    if (localOffset + 30 > archive.length || archive.readUInt32LE(localOffset) !== LOCAL_SIGNATURE) {
      throw new Error('invalid ZIP local header')
    }
    const localNameLength = archive.readUInt16LE(localOffset + 26)
    const localExtraLength = archive.readUInt16LE(localOffset + 28)
    const localHeaderEnd = localOffset + 30 + localNameLength + localExtraLength
    if (localHeaderEnd > archive.length) throw new Error('invalid ZIP local header length')
    const localMember = canonicalMemberPath(
      archive.subarray(localOffset + 30, localOffset + 30 + localNameLength).toString('utf8')
    )
    if (localMember !== member) throw new Error('ZIP local and central member names differ')
    rejectUnsafeExtraFields(archive.subarray(localOffset + 30 + localNameLength, localHeaderEnd))
    const mode = externalAttributes >>> 16
    if ((mode & UNIX_FILE_TYPE) === UNIX_SYMLINK) {
      const rawTarget = readSymlinkTarget(archive, {
        compressedSize,
        compression,
        localOffset,
        member,
        uncompressedSize
      })
      symlinkTarget(member, rawTarget)
    }
    offset = entryEnd
  }
  if (offset !== centralEnd) throw new Error('invalid ZIP central directory size')
  return archive
}

async function validateExtractedTree(root, directory = root) {
  for (const entry of await fs.promises.readdir(directory, { withFileTypes: true })) {
    const target = path.join(directory, entry.name)
    if (entry.isSymbolicLink()) {
      const link = await fs.promises.readlink(target)
      const resolved = path.resolve(directory, link)
      const relative = path.relative(root, resolved)
      if (relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
        throw new Error(`unsafe extracted ZIP symlink: ${JSON.stringify(path.relative(root, target))}`)
      }
    } else if (entry.isDirectory()) {
      await validateExtractedTree(root, target)
    } else if (!entry.isFile()) {
      throw new Error(`unsupported extracted ZIP member: ${JSON.stringify(path.relative(root, target))}`)
    }
  }
}

function resolveExtractorCommand(platform = process.platform, env = process.env, exists = fs.existsSync) {
  if (platform === 'win32') {
    const systemRoot = env.SystemRoot || env.SYSTEMROOT
    if (systemRoot && path.win32.isAbsolute(systemRoot)) {
      const command = path.win32.join(systemRoot, 'System32', 'tar.exe')
      if (exists(command)) {
        return { command, args: (archive, dir) => ['-xf', archive, '-C', dir] }
      }
    }
  } else if (platform === 'darwin') {
    const command = '/usr/bin/tar'
    if (exists(command)) {
      return { command, args: (archive, dir) => ['-xf', archive, '-C', dir] }
    }
  } else {
    for (const command of ['/usr/bin/unzip', '/bin/unzip']) {
      if (exists(command)) {
        return { command, args: (archive, dir) => ['-q', archive, '-d', dir] }
      }
    }
    for (const command of ['/usr/bin/python3', '/usr/local/bin/python3']) {
      if (exists(command)) {
        return {
          command,
          args: (archive, dir) => [PYTHON_EXTRACTOR, archive, dir]
        }
      }
    }
  }

  throw new Error(`trusted ZIP extractor is unavailable for ${platform}`)
}

async function extract(archive, options) {
  const dir = options && options.dir
  if (!path.isAbsolute(archive)) {
    throw new TypeError('archive path must be absolute')
  }
  if (typeof dir !== 'string' || !path.isAbsolute(dir)) {
    throw new TypeError('output directory must be absolute')
  }

  const archiveBytes = await validateZipArchive(archive)
  const requested = path.resolve(dir)
  const parent = path.dirname(requested)
  const name = path.basename(requested)
  if (!name) throw new TypeError('output directory must not be a filesystem root')
  await fs.promises.mkdir(parent, { recursive: true })
  const realParent = await fs.promises.realpath(parent)
  const destination = path.join(realParent, name)
  const existing = await fs.promises.lstat(destination).catch((error) => {
    if (error && error.code === 'ENOENT') return null
    throw error
  })
  if (existing) {
    if (existing.isSymbolicLink() || !existing.isDirectory()) {
      throw new Error('output directory must not be a symlink or non-directory')
    }
    if ((await fs.promises.readdir(destination)).length !== 0) {
      throw new Error('output directory must be empty')
    }
    await fs.promises.rmdir(destination)
  }

  const work = await fs.promises.mkdtemp(path.join(realParent, '.electron-safe-extract-'))
  await fs.promises.chmod(work, 0o700)
  const archiveCopy = path.join(work, 'archive.zip')
  const staged = path.join(work, 'output')
  try {
    await fs.promises.writeFile(archiveCopy, archiveBytes, { flag: 'wx', mode: 0o400 })
    await fs.promises.mkdir(staged, { mode: 0o700 })
    const extractor = resolveExtractorCommand()
    const args = extractor.args(archiveCopy, staged)

    await new Promise((resolve, reject) => {
      const child = spawn(extractor.command, args, {
        shell: false,
        windowsHide: true,
        stdio: ['ignore', 'ignore', 'pipe']
      })
      let stderr = ''
      child.stderr.setEncoding('utf8')
      child.stderr.on('data', (chunk) => {
        if (stderr.length < STDERR_LIMIT) stderr += chunk.slice(0, STDERR_LIMIT - stderr.length)
      })
      child.once('error', reject)
      child.once('close', (code, signal) => {
        if (code === 0) {
          resolve()
          return
        }
        const reason = signal ? `signal ${signal}` : `exit code ${code}`
        reject(new Error(`Electron ZIP extraction failed (${reason})${stderr ? `: ${stderr.trim()}` : ''}`))
      })
    })
    await validateExtractedTree(staged)
    await fs.promises.rename(staged, destination)
  } finally {
    await fs.promises.rm(work, { recursive: true, force: true })
  }
}

module.exports = { extract, resolveExtractorCommand, validateZipArchive }
