// SPDX-License-Identifier: GPL-3.0-only
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}", "eslint-rules/**/*.test.js"],
    coverage: {
      provider: "v8",
      // lcov.info é publicado como artefato no CI e consumido pelo Sonar (TF-143-06).
      reporter: ["text", "html", "lcov"],
      include: ["src/**/*.{ts,tsx}", "eslint-rules/**/*.js"],
      exclude: [
        "src/main.tsx",
        "src/api/schema.gen.ts",
        "src/test/**",
        "**/*.test.{ts,tsx,js}",
        "**/*.d.ts",
      ],
      thresholds: { lines: 90, branches: 85 },
    },
  },
});
