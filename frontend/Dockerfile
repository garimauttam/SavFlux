# ── Stage 1: Build ────────────────────────────────────────────────────────────
# Node 20 to install deps and run `vite build`.
# This stage is ONLY for building — it won't be in the final image.
FROM node:20-slim AS build

WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci --prefer-offline        # ci is faster than install: uses lock file exactly

COPY . .

# VITE_API_URL must be set at BUILD TIME (not runtime) because Vite inlines
# env vars into the JS bundle during `vite build`.
# Pass it as a build arg: docker build --build-arg VITE_API_URL=https://your-backend.railway.app
ARG VITE_API_URL
ARG VITE_SUPABASE_URL
ARG VITE_SUPABASE_ANON_KEY
ENV VITE_API_URL=$VITE_API_URL
ENV VITE_SUPABASE_URL=$VITE_SUPABASE_URL
ENV VITE_SUPABASE_ANON_KEY=$VITE_SUPABASE_ANON_KEY

RUN npm run build                  # produces /app/dist/

# ── Stage 2: Serve ────────────────────────────────────────────────────────────
# nginx:alpine is ~25MB vs node:20-slim at ~200MB.
# We only copy the compiled static files — Node is gone from this image.
FROM nginx:alpine AS runtime

# Copy Vite's build output into nginx's default serve directory
COPY --from=build /app/dist /usr/share/nginx/html

# nginx.conf — handles SPA routing (all paths → index.html)
# Without this, a direct browser hit to /review returns 404 (no file there).
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
