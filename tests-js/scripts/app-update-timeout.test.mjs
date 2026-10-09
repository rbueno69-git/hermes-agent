import { expect, test } from 'vitest'

import { updateReceiptTimeoutMs } from '../../tests/install/e2e-assets/launch-from-spec.mjs'

test('app-driven updates get a 30-minute receipt window unless explicitly overridden', () => {
  expect(updateReceiptTimeoutMs()).toBe(30 * 60_000)
  expect(updateReceiptTimeoutMs('120000')).toBe(120_000)
})
