// SPDX-License-Identifier: GPL-3.0-only
import { QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";

import { errorResponse, sessionHandler, TEST_ORIGIN } from "../test/handlers";
import { metricsSummary } from "../test/metrics-fixtures";
import { server } from "../test/server";

import { useMetricsSummary } from "./metrics";
import { createQueryClient } from "./queryClient";

function wrapper({ children }: Readonly<{ children: ReactNode }>) {
  return <QueryClientProvider client={createQueryClient(0)}>{children}</QueryClientProvider>;
}

describe("useMetricsSummary", () => {
  it("carrega o resumo", async () => {
    server.use(
      sessionHandler(),
      http.get(`${TEST_ORIGIN}/api/metrics/summary`, () => HttpResponse.json(metricsSummary())),
    );

    const { result } = renderHook(() => useMetricsSummary(), { wrapper });

    await waitFor(() => {
      expect(result.current.data?.labels.since_last_training).toBe(15);
    });
  });

  it("atualiza no intervalo pedido", async () => {
    let calls = 0;
    server.use(
      sessionHandler(),
      http.get(`${TEST_ORIGIN}/api/metrics/summary`, () => {
        calls += 1;
        return HttpResponse.json(metricsSummary());
      }),
    );

    renderHook(() => useMetricsSummary(30), { wrapper });

    await waitFor(() => {
      expect(calls).toBeGreaterThanOrEqual(2);
    });
  });

  it("pausa o refetch após uma falha", async () => {
    let calls = 0;
    server.use(
      sessionHandler(),
      http.get(`${TEST_ORIGIN}/api/metrics/summary`, () => {
        calls += 1;
        return errorResponse("INTERNAL", "falha", 400);
      }),
    );

    const { result } = renderHook(() => useMetricsSummary(30), { wrapper });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
    await new Promise((resolve) => setTimeout(resolve, 150));
    expect(calls).toBe(1);
  });
});
