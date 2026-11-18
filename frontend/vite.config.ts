// SPDX-License-Identifier: GPL-3.0-only
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const BACKEND = "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: fileURLToPath(new URL("../src/odc/web/static", import.meta.url)),
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": BACKEND,
      "/auth": BACKEND,
    },
  },
});
