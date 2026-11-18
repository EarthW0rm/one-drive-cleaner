// SPDX-License-Identifier: GPL-3.0-only
import { QueryClient } from "@tanstack/react-query";

import { ApiError } from "./client";

const MAX_RETRIES = 2;

/** Retry só para erros de rede/5xx, no máximo duas vezes; nunca para 4xx. */
export function shouldRetry(failureCount: number, error: unknown): boolean {
  if (failureCount >= MAX_RETRIES) {
    return false;
  }
  return error instanceof ApiError && (error.status === 0 || error.status >= 500);
}

export function createQueryClient(retryDelay?: number): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: shouldRetry,
        ...(retryDelay === undefined ? {} : { retryDelay }),
        refetchOnWindowFocus: false,
      },
    },
  });
}
