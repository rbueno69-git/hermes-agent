import { createRequire } from 'node:module'

export function resolveElectronCommand(callerUrl, entry) {
  const require = createRequire(callerUrl)
  return {
    executable: process.execPath,
    args: [require.resolve('electron/cli.js'), entry],
  }
}
