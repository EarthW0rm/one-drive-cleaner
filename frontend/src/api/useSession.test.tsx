// SPDX-License-Identifier: GPL-3.0-only
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { http } from "msw";
import type { ReactElement } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { errorResponse, sessionHandler, TEST_ORIGIN } from "../test/handlers";
import { server } from "../test/server";

import { createQueryClient } from "./queryClient";
import { useSession } from "./useSession";

function Probe(): ReactElement {
  const { data } = useSession();
  const { pathname } = useLocation();
  return (
    <p>
      {pathname}|{data?.auth_state ?? "carregando"}
    </p>
  );
}

function renderAt(path: string): void {
  render(
    <QueryClientProvider client={createQueryClient(0)}>
      <MemoryRouter initialEntries={[path]}>
        <Probe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("useSession", () => {
  it("mantém a rota quando autenticado", async () => {
    server.use(sessionHandler());

    renderAt("/revisao");

    expect(await screen.findByText("/revisao|authenticated")).toBeInTheDocument();
  });

  it("redireciona para /login com auth_state login_required", async () => {
    server.use(sessionHandler({ auth_state: "login_required", account: null }));

    renderAt("/revisao");

    expect(await screen.findByText("/login|login_required")).toBeInTheDocument();
  });

  it("redireciona para /login com erro LOGIN_REQUIRED", async () => {
    server.use(
      http.get(`${TEST_ORIGIN}/api/session`, () => errorResponse("LOGIN_REQUIRED", "x", 401)),
    );

    renderAt("/metricas");

    await waitFor(() => {
      expect(screen.getByText("/login|carregando")).toBeInTheDocument();
    });
  });

  it("não navega quando já está em /login", async () => {
    server.use(sessionHandler({ auth_state: "login_required", account: null }));

    renderAt("/login");

    expect(await screen.findByText("/login|login_required")).toBeInTheDocument();
  });
});
