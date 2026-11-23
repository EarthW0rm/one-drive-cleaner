// SPDX-License-Identifier: GPL-3.0-only
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError, apiFetch } from "./client";
import type { components } from "./schema.gen";

export type SettingsModel = components["schemas"]["SettingsResponse"];
export type SettingsPatch = components["schemas"]["SettingsPatch"];
export type AutomationMode = SettingsModel["automation_mode"];

export const DEFAULT_AUTOMATION_MODE: AutomationMode = "evolutive";
const SETTINGS_KEY = ["settings"] as const;

const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "[::1]"]);

/** Espelha `require_loopback_endpoint` da API: telemetria nunca sai da máquina (RN-24). */
export function isLoopbackEndpoint(value: string): boolean {
  try {
    const url = new URL(value);
    return (
      (url.protocol === "http:" || url.protocol === "https:") && LOOPBACK_HOSTS.has(url.hostname)
    );
  } catch {
    return false;
  }
}

/**
 * Lê as configurações. Rota ausente (404) devolve `null`: o assistente oculta o passo de
 * automação, útil quando a API local é de uma versão anterior à rota `/api/settings`.
 */
export async function fetchSettings(): Promise<SettingsModel | null> {
  try {
    return await apiFetch<SettingsModel>("/api/settings");
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      return null;
    }
    throw error;
  }
}

async function updateSettings(patch: SettingsPatch): Promise<SettingsModel> {
  return apiFetch<SettingsModel>("/api/settings", { method: "PUT", json: patch });
}

export async function saveAutomationMode(mode: AutomationMode): Promise<void> {
  await updateSettings({ automation_mode: mode });
}

export function useSettings() {
  return useQuery({ queryKey: SETTINGS_KEY, queryFn: fetchSettings });
}

/** Grava uma alteração parcial e atualiza o cache com a resposta da API. */
export function useUpdateSettings() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: updateSettings,
    onSuccess: (saved) => {
      queryClient.setQueryData(SETTINGS_KEY, saved);
    },
  });
}
