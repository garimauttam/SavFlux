import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Vite config — two things worth noting:
// 1. proxy: forwards /api calls to FastAPI during development
//    Without this, the browser would get CORS errors because React runs on :5173
//    and FastAPI on :8000. The proxy makes it look like they're the same origin.
// 2. In production (Vercel), you set the real API URL in an env variable instead.
// Vite 5.4+ rejects any request whose Host header it does not recognise, as a
// DNS-rebinding guard. That is the right default on a laptop, but a dev server
// behind a container proxy or a hosted preview is *always* reached by a
// generated hostname, and the symptom is a 403 with no hint about the cause.
//
// So the allowlist is data, not a hardcoded domain:
//   VITE_ALLOWED_HOSTS=".preview.example.com,my-box.local" npm run dev
// Unset, Vite keeps its own defaults.
const allowedHosts = (process.env.VITE_ALLOWED_HOSTS ?? "")
  .split(",")
  .map((host) => host.trim())
  .filter(Boolean);

export default defineConfig({
  plugins: [react()],
  server: {
    allowedHosts: allowedHosts.length > 0 ? allowedHosts : undefined,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
