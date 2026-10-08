// Fail-closed handling for the native macOS Local Network privacy sheet.
import { execFile } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { promisify } from 'node:util'

const execFileAsync = promisify(execFile)
const scriptPath = path.join(path.dirname(fileURLToPath(import.meta.url)), 'macos-local-network.jxa')

async function nativeProbe(pid) {
  const { stdout } = await execFileAsync('/usr/bin/osascript', ['-l', 'JavaScript', scriptPath, String(pid)], {
    encoding: 'utf8', timeout: 15_000,
  })
  let state
  try {
    state = JSON.parse(stdout.trim())
  } catch (error) {
    throw new Error(`macOS Local Network probe returned invalid evidence: ${stdout.trim()}`, { cause: error })
  }
  if (!Number.isInteger(state.nativeSheets) || !Number.isInteger(state.localNetworkSheets)
      || !Number.isInteger(state.denyButtons) || typeof state.clickedDeny !== 'boolean') {
    throw new Error(`macOS Local Network probe returned an invalid shape: ${stdout.trim()}`)
  }
  return state
}

/**
 * Select “Don’t Allow” if the app owns a Local Network sheet, then require
 * consecutive native probes showing that no such sheet remains.
 */
export async function dismissMacOSLocalNetworkSheet({
  platform = process.platform,
  pid,
  timeoutMs = 30_000,
  clearProbes = 6,
  initialClearProbes = 20,
  probe = nativeProbe,
  sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  now = Date.now,
  pollMs = 500,
}) {
  if (platform !== 'darwin') return { dismissed: false, clearProbes: 0 }
  if (!Number.isInteger(pid) || pid <= 0) throw new Error('a live Electron pid is required for native sheet handling')

  const deadline = now() + timeoutMs
  let consecutiveClear = 0
  let dismissed = false
  for (;;) {
    const state = await probe(pid)
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
