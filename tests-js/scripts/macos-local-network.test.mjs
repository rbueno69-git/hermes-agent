import { expect, test } from 'vitest'

import { clearNativePermissionSheet } from '../../tests/install/e2e-assets/launch-from-spec.mjs'
import { dismissMacOSLocalNetworkSheet } from '../../tests/install/e2e-assets/macos-local-network.mjs'

test('update-window launch passes its exact Electron pid to native permission handling', async () => {
  const seen = []
  await clearNativePermissionSheet({ process: () => ({ pid: 741 }) }, async ({ pid }) => {
    seen.push(pid)
    return { dismissed: true, clearProbes: 2 }
  })
  expect(seen).toEqual([741])
})

test('macOS Local Network handling clicks deny and proves the native sheet stays absent', async () => {
  const states = [
    { nativeSheets: 1, localNetworkSheets: 1, denyButtons: 1, clickedDeny: true },
    { nativeSheets: 0, localNetworkSheets: 0, denyButtons: 0, clickedDeny: false },
    { nativeSheets: 0, localNetworkSheets: 0, denyButtons: 0, clickedDeny: false },
  ]
  let now = 0
  const result = await dismissMacOSLocalNetworkSheet({
    platform: 'darwin', pid: 123, timeoutMs: 1000, clearProbes: 2,
    probe: async () => states.shift(),
    sleep: async () => { now += 10 },
    now: () => now,
  })

  expect(result).toEqual({ dismissed: true, clearProbes: 2 })
  expect(states).toEqual([])
})

test('macOS Local Network handling proves a full initial quiet window when no prompt appears', async () => {
  let probes = 0
  let now = 0
  const result = await dismissMacOSLocalNetworkSheet({
    platform: 'darwin', pid: 123, timeoutMs: 1000, initialClearProbes: 20,
    probe: async () => {
      probes += 1
      return { nativeSheets: 0, localNetworkSheets: 0, denyButtons: 0, clickedDeny: false }
    },
    sleep: async () => { now += 10 },
    now: () => now,
  })

  expect(result).toEqual({ dismissed: false, clearProbes: 20 })
  expect(probes).toBe(20)
})

test('macOS Local Network handling waits through an initial clear window for a late native sheet', async () => {
  const states = [
    { nativeSheets: 0, localNetworkSheets: 0, denyButtons: 0, clickedDeny: false },
    { nativeSheets: 0, localNetworkSheets: 0, denyButtons: 0, clickedDeny: false },
    { nativeSheets: 1, localNetworkSheets: 1, denyButtons: 1, clickedDeny: true },
    ...Array.from({ length: 6 }, () => ({ nativeSheets: 0, localNetworkSheets: 0, denyButtons: 0, clickedDeny: false })),
  ]
  let now = 0
  const result = await dismissMacOSLocalNetworkSheet({
    platform: 'darwin', pid: 123, timeoutMs: 1000,
    probe: async () => states.shift(),
    sleep: async () => { now += 10 },
    now: () => now,
  })

  expect(result).toEqual({ dismissed: true, clearProbes: 6 })
  expect(states).toEqual([])
})

test('macOS Local Network handling rejects any other native sheet before update assertions', async () => {
  await expect(dismissMacOSLocalNetworkSheet({
    platform: 'darwin', pid: 123, timeoutMs: 1000,
    probe: async () => ({ nativeSheets: 1, localNetworkSheets: 0, denyButtons: 0, clickedDeny: false }),
    sleep: async () => {},
  })).rejects.toThrow('unexpected native sheet remains')
})

test('macOS Local Network handling rejects a sheet it could not deny', async () => {
  await expect(dismissMacOSLocalNetworkSheet({
    platform: 'darwin', pid: 123, timeoutMs: 1000,
    probe: async () => ({ nativeSheets: 1, localNetworkSheets: 1, denyButtons: 0, clickedDeny: false }),
    sleep: async () => {},
  })).rejects.toThrow('without an actionable Don’t Allow button')
})

test('macOS Local Network handling never turns a persistent sheet timeout into success', async () => {
  let now = 0
  await expect(dismissMacOSLocalNetworkSheet({
    platform: 'darwin', pid: 123, timeoutMs: 20,
    probe: async () => ({ nativeSheets: 1, localNetworkSheets: 1, denyButtons: 1, clickedDeny: true }),
    sleep: async () => { now += 10 },
    now: () => now,
  })).rejects.toThrow('still present after 20ms')
})

test('non-macOS update harnesses do not invoke the native sheet probe', async () => {
  let probes = 0
  await expect(dismissMacOSLocalNetworkSheet({
    platform: 'linux', pid: 123,
    probe: async () => { probes += 1; throw new Error('must not run') },
  })).resolves.toEqual({ dismissed: false, clearProbes: 0 })
  expect(probes).toBe(0)
})
