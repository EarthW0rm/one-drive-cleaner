// SPDX-License-Identifier: GPL-3.0-only
import { defineConfig, devices } from "@playwright/test";

const CI = Boolean(process.env.CI);

/**
 * Cada teste sobe a sua instância isolada (Graph fake + `odc` real, banco e diretórios
 * temporários) pelo launcher de `tests/e2e_launcher` (ver `support/launcher.ts`): o estado de um
 * cenário, do modelo aprendido e do OneDrive fake nunca vaza para outro, nem entre navegadores.
 */
export default defineConfig({
  testDir: "./specs",
  outputDir: "./test-results",
  workers: 1,
  fullyParallel: false,
  forbidOnly: CI,
  retries: CI ? 1 : 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: CI
    ? [["github"], ["html", { open: "never", outputFolder: "./playwright-report" }]]
    : [["list"]],
  use: { trace: "retain-on-failure", locale: "pt-BR" },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "webkit", use: { ...devices["Desktop Safari"] } },
  ],
});
