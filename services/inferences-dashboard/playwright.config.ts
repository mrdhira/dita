import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { defineConfig, devices } from "@playwright/test";
import { STUB_URL, realWorkerUrl } from "./e2e/worker";

// The whole path: Caddy with the production headers serving dist/, the orchestrator binary
// over a fresh store, and the labelled stub in place of inferences-system-one, unless
// E2E_SYSTEM_ONE_URL names a real worker. Everything else binds 127.0.0.1.
const store = mkdtempSync(join(tmpdir(), "dita-e2e-store-"));
const origin = "https://localhost:8443";

export default defineConfig({
  testDir: "e2e",
  workers: 1,
  fullyParallel: false,
  reporter: [["list"]],
  globalTeardown: "./e2e/teardown.ts",
  use: { baseURL: origin, ignoreHTTPSErrors: true, headless: true },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 900 } },
    },
  ],
  webServer: [
    ...(realWorkerUrl
      ? []
      : [
          {
            command: "node e2e/stub-system-one.mjs",
            url: `${STUB_URL}/health`,
            reuseExistingServer: false,
          },
        ]),
    {
      command: "../dita-orchestrator/bin/dita-orchestrator serveRest",
      url: "http://127.0.0.1:12104/api/inferences/health",
      reuseExistingServer: false,
      env: {
        REST_ADDR: "127.0.0.1:12104",
        DECISIONS_DIR: store,
        INFERENCES_SYSTEM_ONE_URL: realWorkerUrl ?? STUB_URL,
        INFERENCES_EMBEDDING_URL: "http://127.0.0.1:1",
        INFERENCES_RERANKER_URL: "http://127.0.0.1:1",
        DEEPSEEK_API_KEY: "placeholder-e2e-no-chat",
        LOG_LEVEL: "warn",
      },
    },
    {
      command:
        "docker rm -f dita-dashboard-e2e >/dev/null 2>&1; " +
        "exec docker run --rm --name dita-dashboard-e2e --network host " +
        "-e DASHBOARD_ADDR=https://localhost:8443 -e DASHBOARD_BIND=127.0.0.1 " +
        "-e DASHBOARD_API_UPSTREAM=127.0.0.1:12104 " +
        "-v ./Caddyfile:/etc/caddy/Caddyfile:ro -v ./dist:/srv:ro caddy:2.11.4",
      url: `${origin}/`,
      ignoreHTTPSErrors: true,
      reuseExistingServer: false,
      timeout: 60_000,
    },
  ],
});
