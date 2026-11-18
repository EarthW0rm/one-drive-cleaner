// SPDX-License-Identifier: GPL-3.0-only
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type Query,
} from "@tanstack/react-query";
import { useState } from "react";

import { apiFetch } from "./client";
import type { components } from "./schema.gen";

export type JobModel = components["schemas"]["JobModel"];
export type ScanMode = components["schemas"]["ScanRequest"]["mode"];
type JobsResponse = components["schemas"]["JobsResponse"];
type FailuresResponse = components["schemas"]["FailuresResponse"];
type ScanAcceptedResponse = components["schemas"]["ScanAcceptedResponse"];
type RetryFailuresResponse = components["schemas"]["RetryFailuresResponse"];

const JOBS_KEY = ["jobs"] as const;
const FAILURES_KEY = ["failures"] as const;
export const POLL_INTERVAL_MS = 2000;
export const MAX_POLL_ERRORS = 3;
const JOBS_LIMIT = 50;
const FAILURES_PAGE_SIZE = 50;

/** Jobs que ainda andam sozinhos: só eles justificam polling. */
export function hasActiveJob(jobs: readonly JobModel[] | undefined): boolean {
  return jobs?.some((job) => job.status === "running" || job.status === "queued") ?? false;
}

/**
 * Lista os jobs recentes. Faz polling de 2 s somente enquanto houver job `running`/`queued`
 * e para após 3 erros consecutivos (`pollingStopped`); `restartPolling` retoma.
 */
export function useJobs() {
  const [failures, setFailures] = useState(0);
  const query = useQuery({
    queryKey: JOBS_KEY,
    queryFn: async () => {
      try {
        const response = await apiFetch<JobsResponse>(`/api/jobs?limit=${String(JOBS_LIMIT)}`);
        setFailures(0);
        return response.jobs;
      } catch (error) {
        setFailures((count) => count + 1);
        throw error;
      }
    },
    retry: false,
    refetchInterval: (current: Query<JobModel[]>) =>
      failures < MAX_POLL_ERRORS && hasActiveJob(current.state.data) ? POLL_INTERVAL_MS : false,
  });
  const { refetch } = query;
  return {
    ...query,
    pollingStopped: failures >= MAX_POLL_ERRORS,
    restartPolling: () => {
      setFailures(0);
      void refetch();
    },
  };
}

/** Dispara a varredura; o 409 chega como `ApiError` CONFLICT para a tela avisar. */
export function useStartScan() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (mode: ScanMode) =>
      (await apiFetch<ScanAcceptedResponse>("/api/scans", { method: "POST", json: { mode } }))
        .job_id,
    onSettled: () => queryClient.invalidateQueries({ queryKey: JOBS_KEY }),
  });
}

function useJobAction(action: "pause" | "resume") {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (jobId: number) => {
      await apiFetch<unknown>(`/api/jobs/${String(jobId)}/${action}`, { method: "POST" });
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: JOBS_KEY }),
  });
}

export function usePauseJob() {
  return useJobAction("pause");
}

export function useResumeJob() {
  return useJobAction("resume");
}

/** Falhas de análise paginadas por cursor. */
export function useFailures() {
  return useInfiniteQuery({
    queryKey: FAILURES_KEY,
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) => {
      const cursor = pageParam === null ? "" : `&cursor=${encodeURIComponent(pageParam)}`;
      return apiFetch<FailuresResponse>(
        `/api/failures?limit=${String(FAILURES_PAGE_SIZE)}${cursor}`,
      );
    },
    getNextPageParam: (last) => last.next_cursor,
  });
}

export function useRetryFailures() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (itemIds: readonly number[]) =>
      apiFetch<RetryFailuresResponse>("/api/failures/retry", {
        method: "POST",
        json: { item_ids: itemIds },
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: FAILURES_KEY });
      void queryClient.invalidateQueries({ queryKey: JOBS_KEY });
    },
  });
}
