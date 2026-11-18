// SPDX-License-Identifier: GPL-3.0-only
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http } from "msw";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { createQueryClient } from "../api/queryClient";
import { errorResponse, sessionHandler, TEST_ORIGIN } from "../test/handlers";
import { server } from "../test/server";

import { AppRoutes } from "./routes";
import { ThemeContext } from "./theme";

function renderApp(path: string, mode: "light" | "dark" = "light", toggle = () => undefined) {
  return render(
    <ThemeContext value={{ mode, toggle }}>
      <QueryClientProvider client={createQueryClient(0)}>
        <MemoryRouter initialEntries={[path]}>
          <AppRoutes />
        </MemoryRouter>
      </QueryClientProvider>
    </ThemeContext>,
  );
}

describe("AppLayout", () => {
  it("exibe o menu com as 6 telas e navega entre elas", async () => {
    server.use(sessionHandler());
    const user = userEvent.setup();
    renderApp("/revisao");

    expect(screen.getAllByRole("menuitem")).toHaveLength(6);
    for (const label of ["Quarentena", "Categorias", "Varreduras", "Métricas", "Configurações"]) {
      await user.click(screen.getByRole("menuitem", { name: label }));
      expect(await screen.findByRole("heading", { name: label })).toBeInTheDocument();
    }
    await user.click(screen.getByRole("menuitem", { name: "Revisão" }));
    expect(await screen.findByRole("heading", { name: "Revisão" })).toBeInTheDocument();
  });

  it("redireciona / para Revisão e exibe a conta e os placeholders do cabeçalho", async () => {
    server.use(sessionHandler());
    renderApp("/");

    expect(await screen.findByText("Maria Teste")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Revisão" })).toBeInTheDocument();
    expect(screen.getByText("Modo: não definido")).toBeInTheDocument();
    expect(screen.getByLabelText("Alertas")).toBeInTheDocument();
  });

  it("exibe 'Sem conta' quando a sessão não tem conta", async () => {
    server.use(sessionHandler({ account: null }));
    renderApp("/revisao");

    expect(await screen.findByText("Sem conta")).toBeInTheDocument();
  });

  it("aciona a alternância de tema", async () => {
    server.use(sessionHandler());
    let toggled = 0;
    const user = userEvent.setup();
    renderApp("/revisao", "dark", () => {
      toggled += 1;
    });

    await user.click(screen.getByRole("button", { name: "Alternar tema" }));

    expect(toggled).toBe(1);
  });

  it("mostra erro com 'Tentar novamente' quando a API está indisponível", async () => {
    server.use(http.get(`${TEST_ORIGIN}/api/session`, () => errorResponse("INTERNAL", "x", 400)));
    const user = userEvent.setup();
    renderApp("/revisao");

    expect(await screen.findByText("Falha ao carregar a sessão")).toBeInTheDocument();
    server.use(sessionHandler());
    await user.click(screen.getByRole("button", { name: "Tentar novamente" }));

    expect(await screen.findByRole("heading", { name: "Revisão" })).toBeInTheDocument();
  });

  it("vai para /login quando o login é necessário", async () => {
    server.use(sessionHandler({ auth_state: "login_required", account: null }));
    renderApp("/revisao");

    expect(
      await screen.findByRole("heading", { name: "Entrar na conta Microsoft" }),
    ).toBeInTheDocument();
  });

  it("mostra 404 para rota desconhecida", async () => {
    server.use(sessionHandler());
    renderApp("/nada");

    expect(await screen.findByText("Página não encontrada")).toBeInTheDocument();
  });
});
