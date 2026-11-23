// SPDX-License-Identifier: GPL-3.0-only
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { createQueryClient } from "../../api/queryClient";
import { errorResponse, TEST_ORIGIN } from "../../test/handlers";
import { models } from "../../test/review-fixtures";
import { server } from "../../test/server";

import { GateState } from "./GateState";

function renderGate(): void {
  render(
    <QueryClientProvider client={createQueryClient(0)}>
      <GateState />
    </QueryClientProvider>,
  );
}

function serveGate(overrides: Partial<ReturnType<typeof models>["gate"]> = {}): void {
  const base = models();
  server.use(
    http.get(`${TEST_ORIGIN}/api/models`, () =>
      HttpResponse.json({ ...base, gate: { ...base.gate, ...overrides } }),
    ),
  );
}

describe("GateState", () => {
  it("mostra precisão, rótulos, amostras e elegibilidade contra as metas", async () => {
    serveGate();

    renderGate();

    expect(await screen.findByText("0,93 / meta 0,98")).toBeInTheDocument();
    expect(screen.getByText("55 / meta 100")).toBeInTheDocument();
    expect(screen.getByText("12 / meta 30")).toBeInTheDocument();
    expect(screen.getByText("Não")).toBeInTheDocument();
    expect(screen.getByText("Manual")).toBeInTheDocument();
    expect(screen.queryByText("Limiar")).not.toBeInTheDocument();
  });

  it("mostra elegível, modo automático e precisão ausente", async () => {
    serveGate({ eligible: true, effective_mode: "auto", precision: null });

    renderGate();

    expect(await screen.findByText("— / meta 0,98")).toBeInTheDocument();
    expect(screen.getByText("Sim")).toBeInTheDocument();
    expect(screen.getByText("Automático")).toBeInTheDocument();
  });

  it("avisa quando o limiar das métricas diverge do atual", async () => {
    serveGate({ threshold_matches: false });

    renderGate();

    expect(await screen.findByText(/medidas em outro limiar/)).toBeInTheDocument();
  });

  it("indica indisponibilidade sem quebrar quando a API falha", async () => {
    server.use(http.get(`${TEST_ORIGIN}/api/models`, () => errorResponse("INTERNAL", "x", 400)));

    renderGate();

    expect(await screen.findByText("Estado do gate indisponível.")).toBeInTheDocument();
  });
});
