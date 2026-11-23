// SPDX-License-Identifier: GPL-3.0-only
import { http, HttpResponse } from "msw";

import type { MetricsSummary } from "../api/metrics";

import { TEST_ORIGIN } from "./handlers";

export function metricsSummary(overrides: Partial<MetricsSummary> = {}): MetricsSummary {
  return {
    by_state: { DESCOBERTO: 40, ANALISADO: 30, SUGERIDO: 20, QUARENTENA: 10 },
    backlog: { extract: 40, classify: 30, review: 20 },
    quarantine: {
      items: 10,
      bytes: 5 * 1024 * 1024,
      by_category: { "2": 6, "3": 3, unclassified: 1 },
    },
    false_positive_rate: { last_7d: 0.05, total: 0.04 },
    disk: {
      temp: 100 * 1024 * 1024,
      db: 20 * 1024 * 1024,
      model: 5 * 1024 * 1024,
      logs: 1024 * 1024,
      budget_bytes: 500 * 1024 * 1024,
    },
    models: [
      { id: 1, status: "rejected", removable_precision: 0.9, created_at: "2026-01-01T00:00:00Z" },
      { id: 3, status: "current", removable_precision: 0.97, created_at: "2026-01-03T00:00:00Z" },
    ],
    alerts: [],
    labels: { total: 55, since_last_training: 15 },
    ...overrides,
  };
}

export function metricsHandler(summary: MetricsSummary = metricsSummary()) {
  return http.get(`${TEST_ORIGIN}/api/metrics/summary`, () => HttpResponse.json(summary));
}
