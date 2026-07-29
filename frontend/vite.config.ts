import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// Vite dev proxy: the browser calls same-origin "/api/*"; Vite forwards to the
// existing FastAPI service. This is why the frontend needs NO CORS middleware on
// the backend — requests are same-origin from the browser's point of view.
//
// The long proxyTimeout is deliberate. Several backend endpoints block for a very
// long time because they drive a real browser session:
//   POST /dice/connect        ~ up to 11 minutes (waits for manual login)
//   POST /linkedin/connect     ~ minutes
//   POST /applications/apply-selected ~ minutes
// A default proxy timeout (usually 30-120s) would sever these while the automation
// keeps running headless. Keep this well above the largest backend wait (660s).
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const apiTarget = env.VITE_API_TARGET || "http://localhost:8000";

  return {
    // Absolute asset base. This must NOT be relative ("./"): the production build is served
    // by FastAPI and React Router uses real URLs, so a deep route such as /matched-jobs would
    // resolve "./assets/..." against /matched-jobs/ and 404 on every chunk after a refresh.
    base: "/",
    plugins: [react()],
    resolve: {
      alias: { "@": path.resolve(__dirname, "./src") },
    },
    server: {
      host: "127.0.0.1",
      port: 5173,
      strictPort: false,
      proxy: {
        "/api": {
          target: apiTarget,
          changeOrigin: true,
          // Strip the "/api" prefix: "/api/portal/active" -> "/portal/active".
          rewrite: (p) => p.replace(/^\/api/, ""),
          // Do not kill long-running automation calls.
          timeout: 700_000,
          proxyTimeout: 700_000,
        },
      },
    },
    preview: {
      host: "127.0.0.1",
      port: 4173,
    },
    build: {
      // Written to frontend/dist, which FastAPI serves in packaged mode (app/static_site.py).
      outDir: "dist",
      emptyOutDir: true,
      // Split rarely-changing vendor libraries into their own long-cache chunks so a
      // change to app code doesn't bust the whole bundle. Page chunks are produced
      // automatically by the React.lazy() route split in App.tsx.
      rollupOptions: {
        output: {
          manualChunks: {
            "react-vendor": ["react", "react-dom", "react-router-dom"],
            "query-vendor": ["@tanstack/react-query"],
            icons: ["lucide-react"],
          },
        },
      },
    },
  };
});
