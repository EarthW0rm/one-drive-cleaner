// SPDX-License-Identifier: GPL-3.0-only
import { fileURLToPath } from "node:url";

import { defineConfig, devices } from "@playwright/test";

const REPO_ROOT = fileURLToPath(new URL("..", import.meta.url));
const CI = Boolean(process.env.CI);

/**
 * O launcher (tests/e2e_launcher) sobe o Graph fake e o `odc` real em portas aleatórias e imprime
 * `{"app_url": "...", "graph_url": "..."}`; o Playwright captura as URLs por grupos nomeados do
 * regex (viram `ODC_E2E_APP_URL` e `ODC_E2E_GRAPH_URL`) e as fixtures de `fixtures/app.ts` as leem.
 */
const LAUNCHER_READY =
  /"app_url": "(?<odc_e2e_app_url>[^"]+)", "graph_url": "(?<odc_e2e_graph_url>[^"]+)"/;

export default defineConfig({
  testDir: "./specs",
  outputDir: "./test-results",
  // Uma instância do app (estado único em disco e no Graph fake): sem paralelismo.
  workers: 1,
  fullyParallel: false,
  forbidOnly: CI,
  retries: CI ? 1 : 0,
  reporter: CI
    ? [["github"], ["html", { open: "never", outputFolder: "./playwright-report" }]]
    : [["list"]],
  use: { trace: "retain-on-failure", locale: "pt-BR" },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "webkit", use: { ...devices["Desktop Safari"] } },
  ],
  webServer: {
    command: "uv run python -m tests.e2e_launcher.launch",
    cwd: REPO_ROOT,
    wait: { stdout: LAUNCHER_READY },
    timeout: 120_000,
    reuseExistingServer: false,
    gracefulShutdown: { signal: "SIGTERM", timeout: 20_000 },
    stdout: "pipe",
    stderr: "pipe",
  },
});
