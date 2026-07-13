import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "node:path";

// Standalone Vitest config (does not touch vite.config.ts / the dev proxy). Tests run
// in jsdom and talk to MSW, never the real FastAPI backend. This is purely for the
// Phase 5 parity/regression tests — the app's build pipeline is unchanged.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    // Deterministic base URL so MSW resolves the relative "/api/*" fetches.
    environmentOptions: { jsdom: { url: "http://localhost:5173" } },
    css: false,
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
