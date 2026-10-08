// Fail-closed handling for the native macOS Local Network privacy sheet.
import { execFile } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { promisify } from 'node:util'

const execFileAsync = promisify(execFile)
const scriptPath = path.join(path.dirname(fileURLToPath(import.meta.url)), 'macos-local-network.jxa')

async function runJxa(args) {
  const { stdout } = await execFileAsync('/usr/bin/osascript', ['-l', 'JavaScript', scriptPath, ...args], {
    encoding: 'utf8', timeout: 15_000,
  })
  try {
    return JSON.parse(stdout.trim())
  } catch (error) {
    throw new Error(`macOS Local Network probe returned invalid evidence: ${stdout.trim()}`, { cause: error })
  }
}

function canonicalExecutable(executablePath) {
  const resolved = path.resolve(executablePath)
  try {
    return fs.realpathSync.native(resolved)
  } catch {
    return resolved
  }
}

async function nativeIdentityProbe(pid) {
  return runJxa(['identity', String(pid)])
}

export async function captureMacOSProcessIdentity({ pid, executablePath, probe = nativeIdentityProbe }) {
  if (!Number.isInteger(pid) || pid <= 0) throw new Error('a live Electron pid is required for native sheet handling')
  if (typeof executablePath !== 'string' || executablePath.length === 0) {
    throw new Error('the Electron executable path is required for native sheet handling')
  }
  const identity = await probe(pid)
  if (identity?.pid !== pid || !Number.isFinite(identity?.launchTime)
      || typeof identity?.executablePath !== 'string') {
    throw new Error('macOS Local Network identity probe returned an invalid shape')
  }
  const expectedExecutable = canonicalExecutable(executablePath)
  const observedExecutable = canonicalExecutable(identity.executablePath)
  if (observedExecutable !== expectedExecutable) {
    throw new Error(`Electron executable identity changed: expected ${expectedExecutable}, observed ${observedExecutable}`)
  }
  return { pid, launchTime: identity.launchTime, executablePath: observedExecutable }
}

async function nativeProbe(processIdentity) {
  const state = await runJxa([
    'probe',
    String(processIdentity.pid),
    String(processIdentity.launchTime),
    processIdentity.executablePath,
  ])
  if (state?.pid !== processIdentity.pid || state?.launchTime !== processIdentity.launchTime
      || canonicalExecutable(state?.executablePath ?? '') !== processIdentity.executablePath) {
    throw new Error('macOS Local Network probe observed a different process incarnation')
  }
  return state
}

/**
 * Select “Don’t Allow” if the captured app incarnation owns a Local Network
 * sheet, then require consecutive native probes showing that no sheet remains.
 */
export async function dismissMacOSLocalNetworkSheet({
  platform = process.platform,
  processIdentity,
  timeoutMs = 30_000,
  clearProbes = 6,
  initialClearProbes = 20,
  probe = nativeProbe,
  sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  now = Date.now,
  pollMs = 500,
}) {
  if (platform !== 'darwin') return { dismissed: false, clearProbes: 0 }
  if (!Number.isInteger(processIdentity?.pid) || processIdentity.pid <= 0
      || !Number.isFinite(processIdentity?.launchTime)
      || typeof processIdentity?.executablePath !== 'string' || processIdentity.executablePath.length === 0) {
    throw new Error('a captured Electron process identity is required for native sheet handling')
  }

  const deadline = now() + timeoutMs
  let consecutiveClear = 0
  let dismissed = false
  for (;;) {
    const state = await probe(processIdentity)
    if (!Number.isInteger(state.nativeSheets) || !Number.isInteger(state.localNetworkSheets)
        || !Number.isInteger(state.denyButtons) || typeof state.clickedDeny !== 'boolean') {
      throw new Error('macOS Local Network probe returned an invalid shape')
    }
    if (state.nativeSheets > state.localNetworkSheets) {
      throw new Error('unexpected native sheet remains before update assertions')
    }
    if (state.localNetworkSheets > 0) {
      consecutiveClear = 0
      if (state.denyButtons < 1 || !state.clickedDeny) {
        throw new Error('macOS Local Network sheet is present without an actionable Don’t Allow button')
      }
      dismissed = true
    } else {
      consecutiveClear += 1
      const requiredClear = dismissed ? clearProbes : initialClearProbes
      if (consecutiveClear >= requiredClear) {
        return { dismissed, clearProbes: consecutiveClear }
      }
    }

    if (now() >= deadline) {
      throw new Error(`macOS Local Network sheet is still present after ${timeoutMs}ms`)
    }
    await sleep(pollMs)
  }
}
