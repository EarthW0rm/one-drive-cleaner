// SPDX-License-Identifier: GPL-3.0-only
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError, apiFetch } from "./client";
import type { components } from "./schema.gen";

export type CategoryModel = components["schemas"]["CategoryModel"];
export type CategoryCreate = components["schemas"]["CategoryCreate"];
export type CategoryPatch = components["schemas"]["CategoryPatch"];

export const CATEGORIES_KEY = ["categories"] as const;

/**
 * Lista as categorias. Rota ausente (404) devolve `null`: a tela mostra o aviso de lista
 * indisponível em vez de falhar, enquanto a API local for de uma versão sem `/api/categories`.
 */
export async function fetchCategories(): Promise<readonly CategoryModel[] | null> {
  try {
    return (await apiFetch<components["schemas"]["CategoriesResponse"]>("/api/categories"))
      .categories;
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

async function createCategory(body: CategoryCreate): Promise<CategoryModel> {
  return apiFetch<CategoryModel>("/api/categories", { method: "POST", json: body });
}

async function updateCategory(target: {
  readonly id: number;
  readonly patch: CategoryPatch;
}): Promise<CategoryModel> {
  return apiFetch<CategoryModel>(`/api/categories/${String(target.id)}`, {
    method: "PATCH",
    json: target.patch,
  });
}

/** Cria a categoria; a API reclassifica o acervo, então a lista é invalidada. */
export function useCreateCategory() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createCategory,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: CATEGORIES_KEY }),
  });
}

/** Altera campos da categoria (parcial) e recarrega a lista. */
export function useUpdateCategory() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: updateCategory,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: CATEGORIES_KEY }),
  });
}
