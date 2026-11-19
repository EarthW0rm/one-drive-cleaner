// SPDX-License-Identifier: GPL-3.0-only
import { QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { createQueryClient } from "../api/queryClient";
import { SESSION_KEY } from "../api/useSession";
import {
  errorResponse,
  foldersHandler,
  onboardedHandlers,
  scanRootsHandler,
  session,
  sessionHandler,
  TEST_ORIGIN,
} from "../test/handlers";
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
    server.use(
      sessionHandler(),
      http.get(`${TEST_ORIGIN}/api/settings`, () =>
        HttpResponse.json({
          automation_mode: "evolutive",
          auto_threshold: 0.98,
          retrain_every: 20,
          temp_cap_bytes: 2147483648,
          otel_enabled: true,
          otel_endpoint: "http://127.0.0.1:4317",
          restart_required: false,
        }),
      ),
    );
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

  it("sem conta ativa, redireciona para /login sem aviso de expiração", async () => {
    server.use(sessionHandler({ auth_state: "login_required", account: null }));
    renderApp("/revisao");

    expect(
      await screen.findByRole("heading", { name: "Entrar na conta Microsoft" }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Sua sessão expirou/)).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Reconectar" })).not.toBeInTheDocument();
  });

  it("mantém o layout e exibe banner com 'Reconectar' quando a sessão autenticada expira", async () => {
    let calls = 0;
    server.use(
      http.get(`${TEST_ORIGIN}/api/session`, () => {
        calls += 1;
        return HttpResponse.json(
          calls === 1 ? session() : session({ auth_state: "login_required", account: null }),
        );
      }),
    );
    const client = createQueryClient(0);
    render(
      <ThemeContext value={{ mode: "light", toggle: () => undefined }}>
        <QueryClientProvider client={client}>
          <MemoryRouter initialEntries={["/revisao"]}>
            <AppRoutes />
          </MemoryRouter>
        </QueryClientProvider>
      </ThemeContext>,
    );
    expect(await screen.findByText("Maria Teste")).toBeInTheDocument();

    await act(async () => {
      await client.refetchQueries({ queryKey: SESSION_KEY });
    });

    expect(
      await screen.findByText("Sua sessão expirou. Entre novamente para continuar."),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Revisão" })).toBeInTheDocument();
    expect(screen.getByRole("menu")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Reconectar" })).toHaveAttribute("href", "/auth/login");
  });

  it("'Sair' chama o logout, remove a sessão em cache e volta para /login", async () => {
    let logouts = 0;
    server.use(
      sessionHandler(),
      http.post(`${TEST_ORIGIN}/auth/logout`, () => {
        logouts += 1;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    renderApp("/revisao");
    await user.click(await screen.findByRole("button", { name: "Sair" }));

    expect(
      await screen.findByRole("heading", { name: "Entrar na conta Microsoft" }),
    ).toBeInTheDocument();
    expect(logouts).toBe(1);
    expect(screen.queryByText(/Sua sessão expirou/)).not.toBeInTheDocument();
  });

  it("mostra erro quando o logout falha e permanece na tela", async () => {
    server.use(
      sessionHandler(),
      http.post(`${TEST_ORIGIN}/auth/logout`, () => errorResponse("INTERNAL", "x", 500)),
    );
    const user = userEvent.setup();
    renderApp("/revisao");
    await user.click(await screen.findByRole("button", { name: "Sair" }));

    expect(
      await screen.findByText("Ocorreu um erro interno. Tente novamente."),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Revisão" })).toBeInTheDocument();
  });

  it("mostra 404 para rota desconhecida", async () => {
    server.use(sessionHandler());
    renderApp("/nada");

    expect(await screen.findByText("Página não encontrada")).toBeInTheDocument();
  });

  it("redireciona para /onboarding quando não há pasta de varredura", async () => {
    server.use(sessionHandler(), scanRootsHandler([]), foldersHandler());
    renderApp("/revisao");

    expect(
      await screen.findByRole("heading", { name: "Primeira configuração" }),
    ).toBeInTheDocument();
  });

  it("não redireciona quando a primeira configuração já foi concluída", async () => {
    server.use(sessionHandler(), ...onboardedHandlers());
    renderApp("/metricas");

    expect(await screen.findByRole("heading", { name: "Métricas" })).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Primeira configuração" }),
    ).not.toBeInTheDocument();
  });
});
