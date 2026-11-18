// SPDX-License-Identifier: GPL-3.0-only
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { createQueryClient } from "../../api/queryClient";
import {
  errorResponse,
  foldersHandler,
  scanRootsHandler,
  sessionHandler,
  TEST_ORIGIN,
} from "../../test/handlers";
import { server } from "../../test/server";

import { OnboardingPage } from "./OnboardingPage";

const CAMERA_ROOT = { id: 1, display_path: "/Pictures/Camera Roll", is_default: true };

interface Captured {
  roots: unknown[];
  modes: unknown[];
  scans: unknown[];
}

function useBackend(options: { settings: boolean; scanStatus?: number }): Captured {
  const captured: Captured = { roots: [], modes: [], scans: [] };
  server.use(
    sessionHandler(),
    foldersHandler(),
    scanRootsHandler([CAMERA_ROOT]),
    http.put(`${TEST_ORIGIN}/api/scan-roots`, async ({ request }) => {
      captured.roots.push(await request.json());
      return HttpResponse.json({ scan_roots: [CAMERA_ROOT] });
    }),
    http.post(`${TEST_ORIGIN}/api/scans`, async ({ request }) => {
      captured.scans.push(await request.json());
      return options.scanStatus === 409
        ? errorResponse("CONFLICT", "ativa", 409)
        : HttpResponse.json({ job_id: 1 }, { status: 202 });
    }),
    http.get(`${TEST_ORIGIN}/api/settings`, () =>
      options.settings
        ? HttpResponse.json({ automation_mode: "evolutive" })
        : errorResponse("NOT_FOUND", "nao existe", 404),
    ),
    http.put(`${TEST_ORIGIN}/api/settings`, async ({ request }) => {
      captured.modes.push(await request.json());
      return HttpResponse.json({ automation_mode: "manual" });
    }),
  );
  return captured;
}

function renderPage(): void {
  render(
    <QueryClientProvider client={createQueryClient(0)}>
      <MemoryRouter initialEntries={["/onboarding"]}>
        <Routes>
          <Route path="/onboarding" element={<OnboardingPage />} />
          <Route path="/varreduras" element={<h1>Tela Varreduras</h1>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("OnboardingPage", () => {
  it("pré-seleciona a Camera Roll, grava modo e pastas e inicia a varredura", async () => {
    const captured = useBackend({ settings: true });
    const user = userEvent.setup();
    renderPage();

    const next = await screen.findByRole("button", { name: "Avançar" });
    await waitFor(() => {
      expect(next).toBeEnabled();
    });
    await user.click(next);
    await user.click(await screen.findByText("Manual"));
    await user.click(screen.getByRole("button", { name: "Avançar" }));
    expect(screen.getByText(/1 pasta\(s\) selecionada\(s\); modo Manual/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Concluir e iniciar varredura" }));

    expect(await screen.findByRole("heading", { name: "Tela Varreduras" })).toBeInTheDocument();
    expect(captured.roots).toEqual([{ folder_ids: ["FAKE-camera"] }]);
    expect(captured.modes).toEqual([{ automation_mode: "manual" }]);
    expect(captured.scans).toEqual([{ mode: "auto" }]);
  });

  it("mantém Avançar desabilitado sem pastas e oculta o passo de automação sem a rota", async () => {
    const captured = useBackend({ settings: false });
    server.use(scanRootsHandler([]));
    const user = userEvent.setup();
    renderPage();

    const next = await screen.findByRole("button", { name: "Avançar" });
    expect(next).toBeDisabled();
    expect(screen.queryByText("Automação")).not.toBeInTheDocument();

    await user.click(screen.getByRole("combobox"));
    await user.click(await screen.findByText("Documents"));
    await user.click(screen.getByRole("button", { name: "Avançar" }));
    expect(screen.getByText(/1 pasta\(s\) selecionada\(s\)\. A primeira/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Voltar" }));
    await user.click(screen.getByRole("button", { name: "Avançar" }));
    await user.click(screen.getByRole("button", { name: "Concluir e iniciar varredura" }));

    await screen.findByRole("heading", { name: "Tela Varreduras" });
    expect(captured.roots).toEqual([{ folder_ids: ["FAKE-docs"] }]);
    expect(captured.modes).toEqual([]);
  });

  it("trata varredura já ativa (409) como sucesso", async () => {
    useBackend({ settings: false, scanStatus: 409 });
    const user = userEvent.setup();
    renderPage();

    const next = await screen.findByRole("button", { name: "Avançar" });
    await waitFor(() => {
      expect(next).toBeEnabled();
    });
    await user.click(next);
    await user.click(screen.getByRole("button", { name: "Concluir e iniciar varredura" }));

    expect(await screen.findByRole("heading", { name: "Tela Varreduras" })).toBeInTheDocument();
  });

  it("exibe o erro da API ao concluir e permanece na tela", async () => {
    useBackend({ settings: false });
    server.use(
      http.put(`${TEST_ORIGIN}/api/scan-roots`, () => errorResponse("GRAPH_UNAVAILABLE", "x", 503)),
    );
    const user = userEvent.setup();
    renderPage();

    const next = await screen.findByRole("button", { name: "Avançar" });
    await waitFor(() => {
      expect(next).toBeEnabled();
    });
    await user.click(next);
    await user.click(screen.getByRole("button", { name: "Concluir e iniciar varredura" }));

    expect(await screen.findByText(/O OneDrive não respondeu/)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Tela Varreduras" })).not.toBeInTheDocument();
  });

  it("segue sem pré-seleção quando as pastas de varredura não carregam", async () => {
    useBackend({ settings: false });
    server.use(
      http.get(`${TEST_ORIGIN}/api/scan-roots`, () => errorResponse("INTERNAL", "x", 400)),
    );
    renderPage();

    expect(await screen.findByRole("button", { name: "Avançar" })).toBeDisabled();
  });
});
