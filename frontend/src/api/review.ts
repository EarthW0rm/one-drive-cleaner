// SPDX-License-Identifier: GPL-3.0-only
import { useQuery } from "@tanstack/react-query";

import { apiFetch } from "./client";
import type { components } from "./schema.gen";

export type ReviewItem = components["schemas"]["ReviewItem"];
export type ReviewResponse = components["schemas"]["ReviewResponse"];
export type ReviewOrder = "uncertainty" | "confidence";

export const REVIEW_KEY = ["review"] as const;

export interface ReviewQuery {
  readonly categoryId: number | null;
  readonly order: ReviewOrder;
  /** Cursor da página (`null` na primeira). */
  readonly cursor: string | null;
  readonly limit: number;
}

export function reviewKey(query: ReviewQuery) {
  return [...REVIEW_KEY, query] as const;
}

export function reviewPath(query: ReviewQuery): string {
  const params = new URLSearchParams({ order: query.order, limit: String(query.limit) });
  if (query.categoryId !== null) {
    params.set("category_id", String(query.categoryId));
  }
  if (query.cursor !== null) {
    params.set("cursor", query.cursor);
  }
  return `/api/review?${params.toString()}`;
}

/** Fila de revisão (itens `SUGERIDO` sem rótulo), da menor para a maior certeza por padrão. */
export function useReviewPage(query: ReviewQuery) {
  return useQuery({
    queryKey: reviewKey(query),
    queryFn: () => apiFetch<ReviewResponse>(reviewPath(query)),
  });
}
