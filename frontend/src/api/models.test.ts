// SPDX-License-Identifier: GPL-3.0-only
import { describe, expect, it } from "vitest";

import { models } from "../test/review-fixtures";

import { categoryPrecisions, labelsSinceLastTraining, type ModelVersion } from "./models";

function version(id: number, labelsCount: number): ModelVersion {
  return {
    id,
    strategy: "linear",
    status: "candidate",
    trigger: "threshold",
    labels_count: labelsCount,
    created_at: "2026-01-01T00:00:00Z",
    metrics: null,
  };
}

describe("labelsSinceLastTraining", () => {
  it("desconta os rótulos da versão vigente", () => {
    expect(labelsSinceLastTraining(models())).toBe(15);
  });

  it("usa a versão mais recente entre vigente e candidatas", () => {
    const base = models();

    const fresh = labelsSinceLastTraining({
      ...base,
      candidates: [version(9, 50), version(5, 10)],
    });

    expect(fresh).toBe(5);
  });

  it("conta todos os rótulos quando não há nenhuma versão", () => {
    expect(labelsSinceLastTraining({ ...models(), current: null, candidates: [] })).toBe(55);
  });

  it("considera só candidatas quando não há vigente", () => {
    expect(
      labelsSinceLastTraining({ ...models(), current: null, candidates: [version(2, 45)] }),
    ).toBe(10);
  });

  it("nunca devolve valor negativo", () => {
    const base = models();

    expect(labelsSinceLastTraining({ ...base, candidates: [version(8, 999)] })).toBe(0);
  });
});

describe("categoryPrecisions", () => {
  it("lê a precisão por categoria em ordem crescente de id", () => {
    expect(categoryPrecisions(models().current)).toEqual([
      { categoryId: 1, precision: 0.97, support: 30 },
      { categoryId: 2, precision: 0.92, support: 12 },
    ]);
  });

  it("devolve vazio sem modelo, sem métricas ou com formato inesperado", () => {
    const base = models().current;
    const withMetrics = (metrics: Record<string, unknown> | null): ModelVersion | null =>
      base === null ? null : { ...base, metrics };

    expect(categoryPrecisions(null)).toEqual([]);
    expect(categoryPrecisions(withMetrics(null))).toEqual([]);
    expect(categoryPrecisions(withMetrics({ per_category: "x" }))).toEqual([]);
  });

  it("ignora entradas inválidas", () => {
    const base = models().current;
    const metrics = {
      per_category: {
        abc: { precision: 1, support: 1 },
        "3": { precision: "x", support: 1 },
        "4": "inválido",
        "5": { precision: 0.5, support: 2 },
      },
    };

    const scores = categoryPrecisions(base === null ? null : { ...base, metrics });

    expect(scores).toEqual([{ categoryId: 5, precision: 0.5, support: 2 }]);
  });
});
