// SPDX-License-Identifier: GPL-3.0-only
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { createQueryClient } from "../api/queryClient";
import { onboardedHandlers, sessionHandler } from "../test/handlers";
import { metricsHandler } from "../test/metrics-fixtures";
import { quarantineApiHandlers } from "../test/quarantine-fixtures";
import { reviewApiHandlers } from "../test/review-fixtures";
import { server } from "../test/server";

import { AppRoutes } from "./routes";

describe("AppRoutes", () => {
  it("renderiza /login sem o layout e sem chamar a API", () => {
    render(
      <QueryClientProvider client={createQueryClient(0)}>
        <MemoryRouter initialEntries={["/login"]}>
          <AppRoutes />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(screen.getByRole("heading", { name: "Entrar na conta Microsoft" })).toBeInTheDocument();
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("renderiza a tela de Varreduras em /varreduras", async () => {
    server.use(sessionHandler(), ...onboardedHandlers());
    render(
      <QueryClientProvider client={createQueryClient(0)}>
        <MemoryRouter initialEntries={["/varreduras"]}>
          <AppRoutes />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("button", { name: /Varrer agora/ })).toBeInTheDocument();
  });

  it("renderiza a tela de Revisão em /revisao", async () => {
    server.use(sessionHandler(), ...onboardedHandlers(), ...reviewApiHandlers().handlers);
    render(
      <QueryClientProvider client={createQueryClient(0)}>
        <MemoryRouter initialEntries={["/revisao"]}>
          <AppRoutes />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByLabelText(/^Item 1:/)).toBeInTheDocument();
  });

  it("renderiza a tela de Quarentena em /quarentena", async () => {
    server.use(
      sessionHandler(),
      ...onboardedHandlers(),
      ...reviewApiHandlers().handlers,
      ...quarantineApiHandlers().handlers,
    );
    render(
      <QueryClientProvider client={createQueryClient(0)}>
        <MemoryRouter initialEntries={["/quarentena"]}>
          <AppRoutes />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("heading", { name: "Quarentena" })).toBeInTheDocument();
    expect(await screen.findByAltText("Miniatura do item 1")).toBeInTheDocument();
  });

  it("renderiza a tela de Métricas em /metricas", async () => {
    server.use(
      sessionHandler(),
      ...onboardedHandlers(),
      ...reviewApiHandlers().handlers,
      metricsHandler(),
    );
    render(
      <QueryClientProvider client={createQueryClient(0)}>
        <MemoryRouter initialEntries={["/metricas"]}>
          <AppRoutes />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByText("Itens no acervo")).toBeInTheDocument();
  });

  it("renderiza a tela de Categorias em /categorias", async () => {
    server.use(sessionHandler(), ...onboardedHandlers(), ...reviewApiHandlers().handlers);
    render(
      <QueryClientProvider client={createQueryClient(0)}>
        <MemoryRouter initialEntries={["/categorias"]}>
          <AppRoutes />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("heading", { name: "Categorias" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Editar Meme" })).toBeInTheDocument();
  });
});
