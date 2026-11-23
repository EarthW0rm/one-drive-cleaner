// SPDX-License-Identifier: GPL-3.0-only
import { describe, expect, it } from "vitest";

import { models } from "../test/review-fixtures";

import { categoryPrecisions, type ModelVersion } from "./models";

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
