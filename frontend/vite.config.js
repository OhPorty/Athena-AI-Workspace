import { defineConfig } from 'vite';

// base only needs to be '/static/' for the production build -- that's how
// FastAPI actually serves these files (main.py mounts /static as a plain
// StaticFiles directory, separate from the `/` route that reads
// static/index.html directly). In dev mode we want the normal root-relative
// serving Vite already does, so this is a function of `command`, not a
// fixed value.
export default defineConfig(({ command }) => ({
  root: '.',
  base: command === 'build' ? '/static/' : '/',
  publicDir: 'public',
  build: {
    outDir: '../static',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      // Every backend-owned top-level path, confirmed against main.py's
      // own route table (app.include_router calls + the explicit /login
      // route in auth_routes.py) -- everything else the browser asks for
      // is either app.js/index.html/css (served by Vite itself) or a
      // public/ passthrough asset (svg/favicon), which Vite already
      // serves directly in dev mode without needing a proxy.
      '/api': 'http://localhost:9500',
      '/health': 'http://localhost:9500',
      '/login': 'http://localhost:9500',
      '/mcp-ui': 'http://localhost:9500',
      '/mcp-ui-api': 'http://localhost:9500',
    },
  },
}));
