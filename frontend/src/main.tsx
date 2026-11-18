// SPDX-License-Identifier: GPL-3.0-only
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./app/App";

const container = document.getElementById("root");
if (container === null) {
  throw new Error("Elemento #root ausente em index.html");
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
