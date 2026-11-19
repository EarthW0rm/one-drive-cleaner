// SPDX-License-Identifier: GPL-3.0-only
import { useQuery } from "@tanstack/react-query";

import { apiFetch } from "./client";
import type { components } from "./schema.gen";

export type ModelsResponse = components["schemas"]["ModelsResponse"];
export type ModelVersion = components["schemas"]["ModelVersionModel"];

export const MODELS_KEY = ["models"] as const;

export function useModels() {
  return useQuery({
    queryKey: MODELS_KEY,
    queryFn: () => apiFetch<ModelsResponse>("/api/models"),
  });
}

/**
 * Rótulos gravados depois da versão de modelo mais recente (vigente ou candidata). A API não
 * expõe esse contador: ele é aproximado por `gate.labels - labels_count` da última versão.
 */
export function labelsSinceLastTraining(models: ModelsResponse): number {
  const versions =
    models.current === null ? models.candidates : [models.current, ...models.candidates];
  const latest = versions.reduce<ModelVersion | null>(
    (best, version) => (best === null || version.id > best.id ? version : best),
    null,
  );
  return Math.max(0, models.gate.labels - (latest?.labels_count ?? 0));
}

export interface CategoryPrecision {
  readonly categoryId: number;
  readonly precision: number;
  readonly support: number;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/** Precisão por categoria da avaliação do modelo vigente (`metrics.per_category`). */
export function categoryPrecisions(model: ModelVersion | null): CategoryPrecision[] {
  const perCategory = model?.metrics?.per_category;
  if (!isRecord(perCategory)) {
    return [];
  }
  const result: CategoryPrecision[] = [];
  for (const [key, score] of Object.entries(perCategory)) {
    const categoryId = Number(key);
    if (
      Number.isInteger(categoryId) &&
      isRecord(score) &&
      typeof score.precision === "number" &&
      typeof score.support === "number"
    ) {
      result.push({ categoryId, precision: score.precision, support: score.support });
    }
  }
  return result.sort((a, b) => a.categoryId - b.categoryId);
}
