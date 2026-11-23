// SPDX-License-Identifier: GPL-3.0-only
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import type { MetricsSummary } from "../../api/metrics";
import { createQueryClient } from "../../api/queryClient";
import { errorResponse, sessionHandler, TEST_ORIGIN } from "../../test/handlers";
import { metricsHandler, metricsSummary } from "../../test/metrics-fixtures";
import { CATEGORIES } from "../../test/review-fixtures";
import { server } from "../../test/server";

import { ALERT_TEXT, MetricsPage } from "./MetricsPage";

vi.mock("@ant-design/charts", () => ({
  Column: (props: object) => <div data-testid="column">{JSON.stringify(props)}</div>,
  Line: (props: object) => <div data-testid="line">{JSON.stringify(props)}</div>,
  Gauge: (props: object) => <div data-testid="gauge">{JSON.stringify(props)}</div>,
  Pie: (props: object) => <div data-testid="pie">{JSON.stringify(props)}</div>,
}));

function setup(summary: MetricsSummary = metricsSummary()): void {
  server.use(
    sessionHandler(),
    metricsHandler(summary),
    http.get(`${TEST_ORIGIN}/api/categories`, () => HttpResponse.json({ categories: CATEGORIES })),
  );
  render(
    <QueryClientProvider client={createQueryClient(0)}>
      <App>
        <MetricsPage />
      </App>
    </QueryClientProvider>,
  );
}

describe("MetricsPage", () => {
  it("mostra estatísticas, gráficos e uso de disco do resumo", async () => {
    setup();

    expect(await screen.findByTestId("column")).toBeInTheDocument();
    expect(screen.getByText("Itens no acervo")).toBeInTheDocument();
    expect(screen.getByText("100")).toBeInTheDocument();
    expect(screen.getByText("5 MB")).toBeInTheDocument();
    expect(screen.getByText("5%")).toBeInTheDocument();
    expect(screen.getByTestId("line")).toBeInTheDocument();
    expect(screen.getByTestId("gauge")).toHaveTextContent('"target":0.97');
    expect(await screen.findByText(/"category":"Print de tela"/)).toBeInTheDocument();
    expect(screen.getByText(/Linha tracejada: meta de 0,98/)).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: /Uso de disco/ })).toHaveAttribute(
      "aria-valuenow",
      "25",
    );
  });

  it.each(Object.keys(ALERT_TEXT) as (keyof typeof ALERT_TEXT)[])(
    "exibe texto específico para o alerta %s",
    async (code) => {
      const summary = metricsSummary({ alerts: [{ code, since: "2026-01-01T00:00:00Z" }] });
      setup(summary);

      expect(await screen.findByText(ALERT_TEXT[code](summary))).toBeInTheDocument();
    },
  );

  it("exibe — para taxa de falsos positivos nula", async () => {
    setup(metricsSummary({ false_positive_rate: { last_7d: null, total: null } }));

    expect(await screen.findByText("—")).toBeInTheDocument();
  });

  it("oculta os gráficos sem dados com Empty e usa alerta de disco acima do orçamento", async () => {
    const base = metricsSummary();
    setup(
      metricsSummary({
        by_state: {},
        models: [],
        quarantine: { items: 0, bytes: 0, by_category: {} },
        disk: { ...base.disk, temp: base.disk.budget_bytes },
      }),
    );

    expect(await screen.findAllByText("Sem dados para exibir")).toHaveLength(4);
    expect(screen.queryByTestId("column")).not.toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: /Uso de disco/ })).toBeInTheDocument();
  });

  it("mostra erro com Tentar novamente e recupera ao repetir", async () => {
    let fail = true;
    server.use(
      sessionHandler(),
      http.get(`${TEST_ORIGIN}/api/metrics/summary`, () =>
        fail ? errorResponse("INTERNAL", "x", 400) : HttpResponse.json(metricsSummary()),
      ),
      http.get(`${TEST_ORIGIN}/api/categories`, () => HttpResponse.json({ categories: [] })),
    );
    render(
      <QueryClientProvider client={createQueryClient(0)}>
        <App>
          <MetricsPage />
        </App>
      </QueryClientProvider>,
    );
    const retry = await screen.findByRole("button", { name: "Tentar novamente" });
    fail = false;

    await userEvent.setup().click(retry);

    expect(await screen.findByTestId("column")).toBeInTheDocument();
    expect(screen.queryByText("Falha ao carregar as métricas")).not.toBeInTheDocument();
  });
});
