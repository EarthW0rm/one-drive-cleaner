// SPDX-License-Identifier: GPL-3.0-only
import { useQuery } from "@tanstack/react-query";

import { ApiError, apiFetch } from "./client";

/** Contrato de `GET /api/categories` (plan 4.1); a rota chega com a TF-154-01. */
export interface CategoryModel {
  readonly id: number;
  readonly slug: string;
  readonly name: string;
  readonly removable: boolean;
  readonly protected: boolean;
  readonly archived: boolean;
  readonly labels: number;
}

interface CategoriesResponse {
  readonly categories: readonly CategoryModel[];
}

export const CATEGORIES_KEY = ["categories"] as const;

/**
 * Lista as categorias. Rota ausente (404) devolve `null`: a tela mostra o aviso de lista
 * indisponível em vez de falhar, enquanto a API local for de uma versão sem `/api/categories`.
 */
export async function fetchCategories(): Promise<readonly CategoryModel[] | null> {
  try {
    return (await apiFetch<CategoriesResponse>("/api/categories")).categories;
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      return null;
    }
    throw error;
  }
}

export function useCategories() {
  return useQuery({ queryKey: CATEGORIES_KEY, queryFn: fetchCategories });
}
