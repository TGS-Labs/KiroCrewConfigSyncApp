import path from 'node:path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Separate from vite.config.ts (the library-build config) because vitest's
// `test` block is not a valid Rollup/Vite build option and would otherwise
// have to be merged into the build config every app in this repo ships.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      // `@kirocrew/app-sdk[/ui]` and `lucide-react` are host-injected
      // globals at runtime (see App.test.tsx's top-of-file comment) —
      // there is no real npm package to install for them, so Vite's
      // static import analysis has nothing on disk to resolve
      // `import ... from '@kirocrew/app-sdk'` against. These aliases
      // point the bare specifiers at trivial on-disk stub modules purely
      // so the build graph resolves; the actual behaviour under test
      // always comes from each test file's own `vi.mock(...)`, which
      // vitest hoists above the module graph and applies before a stub's
      // exports would ever run.
      '@kirocrew/app-sdk/ui': path.resolve(__dirname, 'src/test-stubs/app-sdk-ui.ts'),
      '@kirocrew/app-sdk': path.resolve(__dirname, 'src/test-stubs/app-sdk.ts'),
      'lucide-react': path.resolve(__dirname, 'src/test-stubs/lucide-react.ts'),
    },
  },
  test: {
    environment: 'jsdom',
    globals: false,
    setupFiles: ['./vitest.setup.ts'],
  },
})
