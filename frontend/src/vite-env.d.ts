/// <reference types="vite/client" />

/**
 * Type declarations for Vite's import.meta.env.
 *
 * WHY THIS FILE?
 * Vite replaces `import.meta.env.VITE_*` at build time.
 * Without this file, TypeScript doesn't know those variables exist and
 * you'd get "Property 'VITE_API_URL' does not exist" type errors.
 *
 * Only VITE_* prefixed variables are exposed to the browser bundle.
 * Anything without that prefix stays server-side only.
 */
interface ImportMetaEnv {
  /** Base URL of the FastAPI backend. Set to your Railway URL in Vercel's env vars. */
  readonly VITE_API_URL: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
