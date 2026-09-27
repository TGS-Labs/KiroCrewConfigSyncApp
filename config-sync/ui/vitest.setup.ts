import '@testing-library/jest-dom/vitest'
import { afterEach } from 'vitest'
import { cleanup } from '@testing-library/react'

// `vitest.config.ts` sets `globals: false`, so @testing-library/react's own
// auto-cleanup registration (which relies on a global `afterEach`) never
// fires and every test's rendered tree piles up in the same jsdom `document`
// across the whole file — leading to "found multiple elements" failures that
// have nothing to do with App.tsx's own correctness. Register cleanup
// explicitly against vitest's imported `afterEach` instead.
afterEach(() => {
  cleanup()
})
