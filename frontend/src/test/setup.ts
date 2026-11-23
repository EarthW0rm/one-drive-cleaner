// SPDX-License-Identifier: GPL-3.0-only
import "@testing-library/jest-dom/vitest";

import { cleanup, configure } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, vi } from "vitest";

import { resetCsrfToken } from "../api/client";

import { server } from "./server";

// jsdom não implementa matchMedia (usado pelo antd e pelo tema).
Object.defineProperty(window, "matchMedia", {
  writable: true,
  configurable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false,
  }),
});

// jsdom não implementa ResizeObserver (usado pelos componentes de seleção do antd).
class ResizeObserverStub {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}
window.ResizeObserver = ResizeObserverStub;

// Runners compartilhados (CI) renderizam o antd e resolvem o MSW bem mais devagar que a máquina
// local; o padrão de 1 s de findBy*/waitFor gerava falha intermitente sem relação com a lógica.
// Abaixo do timeout de 5 s do vitest, para que o erro do Testing Library apareça antes dele.
configure({ asyncUtilTimeout: 4000 });

// Nenhum teste acessa a rede real: requisições sem handler explícito falham.
beforeAll(() => {
  server.listen({ onUnhandledFrame: "error" });
});

afterEach(() => {
  cleanup();
  resetCsrfToken();
  window.localStorage.clear();
  server.resetHandlers();
});

afterAll(() => {
  server.close();
});
