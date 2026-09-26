/**
 * vitest.config.ts — test config, deliberately separate from vite.config.ts.
 *
 * The dev config carries the /api proxy and the allowedHosts guard, neither of
 * which means anything under jsdom. Keeping them apart means a change to the dev
 * server can never silently change what the tests see, and `npm test` works with
 * no backend running — a UI test must not need a live API.
 */
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    css: false,
  },
})
