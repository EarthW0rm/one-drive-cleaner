// SPDX-License-Identifier: GPL-3.0-only
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { createQueryClient } from "../api/queryClient";
import { onboardedHandlers, sessionHandler } from "../test/handlers";
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
});
