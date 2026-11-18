// SPDX-License-Identifier: GPL-3.0-only
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiFetch, ApiError } from "./client";
import type { components } from "./schema.gen";
import { saveAutomationMode, type AutomationMode } from "./settings";

export type FolderModel = components["schemas"]["FolderModel"];
export type ScanRootModel = components["schemas"]["ScanRootModel"];
type FoldersResponse = components["schemas"]["FoldersResponse"];
type ScanRootsResponse = components["schemas"]["ScanRootsResponse"];
type JobsResponse = components["schemas"]["JobsResponse"];
type ScanAcceptedResponse = components["schemas"]["ScanAcceptedResponse"];

export const SCAN_ROOTS_KEY = ["scan-roots"] as const;
export const NEEDS_ONBOARDING_KEY = ["needs-onboarding"] as const;

/** Subpastas imediatas; sem `parentId` lista a raiz do OneDrive. */
export async function fetchFolders(parentId?: string): Promise<FolderModel[]> {
  const query = parentId === undefined ? "" : `?parent_id=${encodeURIComponent(parentId)}`;
  const response = await apiFetch<FoldersResponse>(`/api/folders${query}`);
  return response.folders;
}

async function fetchScanRoots(): Promise<ScanRootModel[]> {
  return (await apiFetch<ScanRootsResponse>("/api/scan-roots")).scan_roots;
}

export function useScanRoots(enabled = true) {
  return useQuery({ queryKey: SCAN_ROOTS_KEY, queryFn: fetchScanRoots, enabled });
}

async function saveScanRoots(folderIds: readonly string[]): Promise<ScanRootModel[]> {
  const response = await apiFetch<ScanRootsResponse>("/api/scan-roots", {
    method: "PUT",
    json: { folder_ids: folderIds },
  });
  return response.scan_roots;
}

/** Inicia a varredura; 409 (já existe uma ativa) conta como sucesso. */
export async function startScan(): Promise<void> {
  try {
    await apiFetch<ScanAcceptedResponse>("/api/scans", { method: "POST", json: { mode: "auto" } });
  } catch (error) {
    if (!(error instanceof ApiError && error.code === "CONFLICT")) {
      throw error;
    }
  }
}

/**
 * A primeira configuração está pendente quando não há pasta, ou só existe a Camera Roll
 * padrão cadastrada automaticamente e nenhuma varredura foi iniciada ainda.
 */
export async function fetchNeedsOnboarding(): Promise<boolean> {
  const roots = await fetchScanRoots();
  if (roots.length === 0) {
    return true;
  }
  if (roots.some((root) => !root.is_default)) {
    return false;
  }
  const jobs = await apiFetch<JobsResponse>("/api/jobs?type=scan&limit=1");
  return jobs.jobs.length === 0;
}

export function useNeedsOnboarding(enabled: boolean) {
  return useQuery({
    queryKey: NEEDS_ONBOARDING_KEY,
    queryFn: fetchNeedsOnboarding,
    enabled,
    staleTime: Infinity,
  });
}

export interface CompleteOnboardingInput {
  readonly folderIds: readonly string[];
  /** `null` quando a rota de configurações ainda não existe (passo oculto). */
  readonly automationMode: AutomationMode | null;
}

/** Grava pastas e modo e inicia a primeira varredura. */
export function useCompleteOnboarding() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ folderIds, automationMode }: CompleteOnboardingInput) => {
      await saveScanRoots(folderIds);
      if (automationMode !== null) {
        await saveAutomationMode(automationMode);
      }
      await startScan();
    },
    onSuccess: () => {
      queryClient.setQueryData(NEEDS_ONBOARDING_KEY, false);
      void queryClient.invalidateQueries({ queryKey: SCAN_ROOTS_KEY });
    },
  });
}
