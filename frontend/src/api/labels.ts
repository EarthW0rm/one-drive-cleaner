// SPDX-License-Identifier: GPL-3.0-only
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { apiFetch } from "./client";
import { METRICS_KEY } from "./metrics";
import { MODELS_KEY } from "./models";
import type { components } from "./schema.gen";

export type LabelEntry = components["schemas"]["LabelEntry"];
export type LabelsResponse = components["schemas"]["LabelsResponse"];

/** Grava confirmações e correções como rótulos (RN-11); tudo-ou-nada no servidor. */
export function useSaveLabels() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (labels: readonly LabelEntry[]) =>
      apiFetch<LabelsResponse>("/api/labels", { method: "POST", json: { labels } }),
    onSuccess: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: MODELS_KEY }),
        queryClient.invalidateQueries({ queryKey: METRICS_KEY }),
      ]),
  });
}
