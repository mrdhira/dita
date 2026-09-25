import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The dev server proxies /api the way Caddy does in deployment, so the SPA only ever
// speaks to its own origin.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: "127.0.0.1",
    proxy: { "/api": process.env.DASHBOARD_API ?? "http://127.0.0.1:2104" },
  },
  build: {
    // No inline scripts or data: URIs: the CSP allows 'self' and nothing else.
    assetsInlineLimit: 0,
    modulePreload: { polyfill: false },
    sourcemap: false,
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: ["src/test/setup.ts"],
    // Above the 5 s async wait in setup.ts, so a wait that runs out fails with its own message.
    testTimeout: 15_000,
    coverage: {
      provider: "v8",
      include: ["src/**/*.{ts,tsx}"],
      exclude: ["src/**/*.test.{ts,tsx}", "src/test/**", "src/main.tsx"],
      // A tripwire, not a target: set just under what the suite reaches.
      thresholds: { statements: 92, branches: 84, functions: 91, lines: 93 },
    },
  },
});
