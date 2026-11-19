// SPDX-License-Identifier: GPL-3.0-only
import { useQuery } from "@tanstack/react-query";

import { apiFetch } from "./client";
import type { components } from "./schema.gen";

export type MetricsSummary = components["schemas"]["MetricsSummaryResponse"];
export type MetricsAlert = components["schemas"]["AlertModel"];
export type AlertCode = MetricsAlert["code"];

export const METRICS_KEY = ["metrics", "summary"] as const;
export const METRICS_REFRESH_MS = 15_000;

/**
 * Resumo de métricas locais. Com `refreshMs`, atualiza nesse intervalo enquanto a página está
 * visível (sem refetch em segundo plano) e pausa após uma falha, até um novo `refetch` manual.
 */
export function useMetricsSummary(refreshMs: number | false = false) {
  return useQuery({
    queryKey: METRICS_KEY,
    queryFn: () => apiFetch<MetricsSummary>("/api/metrics/summary"),
    refetchInterval: (query) => (query.state.status === "error" ? false : refreshMs),
    refetchIntervalInBackground: false,
  });
}
