// SPDX-License-Identifier: GPL-3.0-only
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiFetch } from "./client";
import { MODELS_KEY } from "./models";
import { REVIEW_KEY } from "./review";
import type { components } from "./schema.gen";

export type QuarantineItem = components["schemas"]["QuarantineItem"];
export type RestoreNotice = components["schemas"]["RestoreNotice"];
export type QuarantineResponse = components["schemas"]["QuarantineResponse"];
export type RestoreEntry = components["schemas"]["RestoreEntry"];
export type DeletionPreview = components["schemas"]["DeletionPreview"];

type JobResponse = components["schemas"]["JobResponse"];

export const QUARANTINE_KEY = ["quarantine"] as const;

/** Limite de itens por restauração (`MAX_RESTORES` no servidor). */
export const MAX_RESTORES = 500;

export interface QuarantineQuery {
  readonly categoryId: number | null;
  /** Cursor da página (`null` na primeira). */
  readonly cursor: string | null;
  readonly limit: number;
}

function quarantineKey(query: QuarantineQuery) {
  return [...QUARANTINE_KEY, query] as const;
}

export function quarantinePath(query: QuarantineQuery): string {
  const params = new URLSearchParams({ limit: String(query.limit) });
  if (query.categoryId !== null) {
    params.set("category_id", String(query.categoryId));
  }
  if (query.cursor !== null) {
    params.set("cursor", query.cursor);
  }
  return `/api/quarantine?${params.toString()}`;
}

/** Itens em quarentena (mais recentes primeiro), grupos por categoria e avisos de restauração. */
export function useQuarantine(query: QuarantineQuery) {
  return useQuery({
    queryKey: quarantineKey(query),
    queryFn: () => apiFetch<QuarantineResponse>(quarantinePath(query)),
  });
}

/** Enfileira a movimentação manual (RN-09: 422 com `details.blocked_item_ids` se bloqueada). */
export function useMoveToQuarantine() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (itemIds: readonly number[]) =>
      (
        await apiFetch<JobResponse>("/api/quarantine/moves", {
          method: "POST",
          json: { item_ids: itemIds },
        })
      ).job_id,
    onSuccess: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: REVIEW_KEY }),
        queryClient.invalidateQueries({ queryKey: QUARANTINE_KEY }),
      ]),
  });
}

/** Restaura com a categoria correta (gera rótulo e, se couber, retreino no servidor). */
export function useRestoreFromQuarantine() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (restores: readonly RestoreEntry[]) =>
      (
        await apiFetch<JobResponse>("/api/quarantine/restores", {
          method: "POST",
          json: { restores },
        })
      ).job_id,
    onSuccess: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: QUARANTINE_KEY }),
        queryClient.invalidateQueries({ queryKey: MODELS_KEY }),
      ]),
  });
}

/** Passo 1 da exclusão (RN-20): quantidade, volume e token de uso único. */
export function usePreviewDeletion() {
  return useMutation({
    mutationFn: (itemIds: readonly number[]) =>
      apiFetch<DeletionPreview>("/api/quarantine/deletions/preview", {
        method: "POST",
        json: { item_ids: itemIds },
      }),
  });
}

/** Passo 2: confirma com o token; os itens vão para a lixeira do OneDrive em segundo plano. */
export function useConfirmDeletion() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (request: {
      readonly itemIds: readonly number[];
      readonly confirmationToken: string;
    }) =>
      (
        await apiFetch<JobResponse>("/api/quarantine/deletions", {
          method: "POST",
          json: { item_ids: request.itemIds, confirmation_token: request.confirmationToken },
        })
      ).job_id,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: QUARANTINE_KEY }),
  });
}
