/**
 * Test setup — the browser APIs jsdom does not implement, plus jest-dom.
 *
 * The scroll stubs are assigned unconditionally rather than "if missing", so a
 * test can always `vi.spyOn(Element.prototype, "scrollIntoView")`: spying needs
 * an own property on the prototype, and jsdom ships none for these.
 * `vi.restoreAllMocks()` (afterEach) hands them back to the plain stubs.
 */
import '@testing-library/jest-dom/vitest'
import { afterEach, vi } from 'vitest'
import { cleanup } from '@testing-library/react'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

// Distinct stubs: three separate fns, so asserting on one never picks up calls
// made through another.
for (const name of ['scrollIntoView', 'scrollBy', 'scrollTo'] as const) {
  Object.defineProperty(Element.prototype, name, {
    value: vi.fn(),
    writable: true,
    configurable: true,
  })
}

// ThemeToggle calls matchMedia on mount; jsdom does not provide it.
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })) as unknown as typeof window.matchMedia
}
