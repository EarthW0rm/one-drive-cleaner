// SPDX-License-Identifier: GPL-3.0-only
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { createQueryClient } from "../../api/queryClient";
import { errorResponse, sessionHandler, TEST_ORIGIN } from "../../test/handlers";
import { metricsHandler } from "../../test/metrics-fixtures";
import { server } from "../../test/server";

import { PAUSE_REASON_TEXT, ScansPage } from "./ScansPage";

interface JobFixture {
  id: number;
  type: string;
  status: string;
  progress: { done: number; total: number };
  pause_reason: string | null;
  error_code: string | null;
}

function job(overrides: Partial<JobFixture> = {}): JobFixture {
  return {
    id: 1,
    type: "scan",
    status: "done",
    progress: { done: 2, total: 2 },
    pause_reason: null,
    error_code: null,
    ...overrides,
  };
}

const NO_FAILURES = http.get(`${TEST_ORIGIN}/api/failures`, () =>
  HttpResponse.json({ items: [], next_cursor: null }),
);

function useJobs(jobs: JobFixture[], ...extra: Parameters<typeof server.use>): void {
  server.use(
    sessionHandler(),
    http.get(`${TEST_ORIGIN}/api/jobs`, () => HttpResponse.json({ jobs })),
    NO_FAILURES,
    ...extra,
  );
}

function renderPage(): void {
  render(
    <QueryClientProvider client={createQueryClient(0)}>
      <ScansPage />
    </QueryClientProvider>,
  );
}

describe("ScansPage", () => {
  it("mostra a última varredura, o progresso e os tipos de job", async () => {
    useJobs([
      job({ id: 3, type: "extract", status: "running", progress: { done: 0, total: 0 } }),
      job({ id: 2, status: "running", progress: { done: 1, total: 4 } }),
      job({ id: 1, type: "desconhecido", status: "estranho", error_code: "INTERNAL" }),
    ]);
    renderPage();

    expect(await screen.findByText("Extração")).toBeInTheDocument();
    expect(screen.getByText("1 de 4")).toBeInTheDocument();
    expect(
      screen.getAllByRole("progressbar").map((bar) => bar.getAttribute("aria-valuenow")),
    ).toEqual(["25", "100"]);
    expect(
      screen.getByRole("progressbar", { name: "Progresso da varredura: 1 de 4" }),
    ).toBeInTheDocument();
    expect(screen.getByText("desconhecido")).toBeInTheDocument();
    expect(screen.getByText("estranho")).toBeInTheDocument();
    expect(screen.getByText("INTERNAL")).toBeInTheDocument();
  });

  it("exibe contadores por estado e backlog do resumo de métricas", async () => {
    useJobs([job()], metricsHandler());
    renderPage();

    expect(await screen.findByText("Aguardando extração")).toBeInTheDocument();
    expect(screen.getByText("Sugerido")).toBeInTheDocument();
    expect(screen.getByText("Aguardando revisão")).toBeInTheDocument();
  });

  it("indica quando nunca houve varredura", async () => {
    useJobs([]);
    renderPage();

    expect(await screen.findByText("Nenhuma varredura executada")).toBeInTheDocument();
    expect(screen.getByText("Nenhum job registrado")).toBeInTheDocument();
  });

  it.each(["login_required", "temp_cap", "network", "user"])(
    "exibe texto específico para pause_reason %s",
    async (reason) => {
      useJobs([job({ status: "paused", pause_reason: reason })]);
      renderPage();

      expect(await screen.findByText(PAUSE_REASON_TEXT[reason] ?? "")).toBeInTheDocument();
    },
  );

  it("usa texto genérico para pause_reason desconhecido", async () => {
    useJobs([job({ status: "paused", pause_reason: "outro" })]);
    renderPage();

    expect(await screen.findByText("Pausado por um motivo desconhecido.")).toBeInTheDocument();
  });

  it("pausa um job em andamento e retoma um pausado", async () => {
    const calls: string[] = [];
    useJobs(
      [job({ id: 5, status: "running" }), job({ id: 6, status: "paused", pause_reason: "user" })],
      http.post(`${TEST_ORIGIN}/api/jobs/:id/:action`, ({ params }) => {
        calls.push(`${String(params.id)}/${String(params.action)}`);
        return HttpResponse.json({});
      }),
    );
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Pausar" }));
    await user.click(screen.getByRole("button", { name: "Retomar" }));

    await waitFor(() => {
      expect(calls).toEqual(["5/pause", "6/resume"]);
    });
  });

  it("mostra erro quando pausar falha", async () => {
    useJobs(
      [job({ id: 5, status: "running" })],
      http.post(`${TEST_ORIGIN}/api/jobs/5/pause`, () => errorResponse("CONFLICT", "estado", 409)),
    );
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Pausar" }));

    expect(await screen.findByText(/conflita com o estado atual/)).toBeInTheDocument();
  });

  it("inicia varredura automática e completa", async () => {
    const modes: unknown[] = [];
    useJobs(
      [],
      http.post(`${TEST_ORIGIN}/api/scans`, async ({ request }) => {
        modes.push(await request.json());
        return HttpResponse.json({ job_id: 9 }, { status: 202 });
      }),
    );
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: /Varrer agora/ }));
    expect(await screen.findByText("Varredura iniciada.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Varredura completa/ }));

    await waitFor(() => {
      expect(modes).toEqual([{ mode: "auto" }, { mode: "full" }]);
    });
  });

  it("avisa quando já há varredura em andamento (409)", async () => {
    useJobs(
      [],
      http.post(`${TEST_ORIGIN}/api/scans`, () => errorResponse("CONFLICT", "ativa", 409)),
    );
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: /Varrer agora/ }));

    expect(await screen.findByText("Varredura já em andamento")).toBeInTheDocument();
  });

  it("mostra o erro de outras falhas ao iniciar", async () => {
    useJobs(
      [],
      http.post(`${TEST_ORIGIN}/api/scans`, () => errorResponse("GRAPH_UNAVAILABLE", "x", 503)),
    );
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: /Varrer agora/ }));

    expect(await screen.findByText(/O OneDrive não respondeu/)).toBeInTheDocument();
  });

  it("mostra erro de carregamento dos jobs", async () => {
    server.use(
      sessionHandler(),
      http.get(`${TEST_ORIGIN}/api/jobs`, () => errorResponse("INTERNAL", "x", 500)),
      NO_FAILURES,
    );
    renderPage();

    expect(await screen.findByText(/erro interno/)).toBeInTheDocument();
  });
});
