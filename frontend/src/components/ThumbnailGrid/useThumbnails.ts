// SPDX-License-Identifier: GPL-3.0-only
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";

import { apiFetch } from "../../api/client";
import type { components } from "../../api/schema.gen";

type ThumbnailsResponse = components["schemas"]["ThumbnailsResponse"];

/** URLs pré-autenticadas por `item_id` (apenas as que o OneDrive devolveu). */
type ThumbnailUrls = Readonly<Record<string, string>>;

const THUMBNAILS_KEY = "thumbnails";
const STALE_MS = 10 * 60 * 1000;

async function fetchThumbnails(
  itemIds: readonly number[],
  refresh: boolean,
): Promise<ThumbnailUrls> {
  const response = await apiFetch<ThumbnailsResponse>("/api/thumbnails", {
    method: "POST",
    json: { item_ids: itemIds, refresh },
  });
  return response.urls;
}

/** Pede de novo (uma vez) as URLs que o servidor não devolveu; falha aqui não derruba a grade. */
async function fetchWithSingleRetry(itemIds: readonly number[]): Promise<ThumbnailUrls> {
  const first = await fetchThumbnails(itemIds, false);
  const missing = itemIds.filter((id) => !(String(id) in first));
  if (missing.length === 0) {
    return first;
  }
  try {
    return { ...first, ...(await fetchThumbnails(missing, true)) };
  } catch {
    return first;
  }
}

interface IdSet {
  readonly key: string;
  readonly ids: ReadonlySet<number>;
}

export interface Thumbnails {
  readonly urls: ThumbnailUrls;
  readonly isLoading: boolean;
  readonly isError: boolean;
  /** Itens cuja miniatura continua falhando após a nova tentativa (mostram placeholder). */
  readonly failed: ReadonlySet<number>;
  /** Chamar no `onError` da imagem: refaz a URL uma única vez por item e depois desiste. */
  readonly onImageError: (itemId: number) => void;
}

/**
 * URLs de miniatura da página atual em uma chamada (a API busca no Graph em lotes de 20).
 * Nada é baixado via `fetch`: a interface usa a URL direto em `<img src>` (RN-02).
 */
export function useThumbnails(itemIds: readonly number[]): Thumbnails {
  const queryClient = useQueryClient();
  const idsKey = itemIds.join(",");
  const queryKey = [THUMBNAILS_KEY, idsKey] as const;
  const query = useQuery({
    queryKey,
    queryFn: () => fetchWithSingleRetry(itemIds),
    enabled: itemIds.length > 0,
    staleTime: STALE_MS,
  });
  const [failedState, setFailedState] = useState<IdSet>({ key: idsKey, ids: new Set() });
  const retried = useRef<{ key: string; ids: Set<number> }>({ key: idsKey, ids: new Set() });

  function markFailed(itemId: number): void {
    setFailedState((state) => ({
      key: idsKey,
      ids: new Set(state.key === idsKey ? state.ids : []).add(itemId),
    }));
  }

  function onImageError(itemId: number): void {
    if (retried.current.key !== idsKey) {
      retried.current = { key: idsKey, ids: new Set() };
    }
    if (retried.current.ids.has(itemId)) {
      markFailed(itemId);
      return;
    }
    retried.current.ids.add(itemId);
    void fetchThumbnails([itemId], true).then(
      (fresh) => {
        const url = fresh[String(itemId)];
        if (url === undefined) {
          markFailed(itemId);
          return;
        }
        queryClient.setQueryData<ThumbnailUrls>(queryKey, (old) => ({
          ...old,
          [String(itemId)]: url,
        }));
      },
      () => {
        markFailed(itemId);
      },
    );
  }

  return {
    urls: query.data ?? {},
    isLoading: query.isPending && itemIds.length > 0,
    isError: query.isError,
    failed: failedState.key === idsKey ? failedState.ids : new Set(),
    onImageError,
  };
}
