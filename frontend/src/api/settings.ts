// SPDX-License-Identifier: GPL-3.0-only
import { useQuery } from "@tanstack/react-query";

import { ApiError, apiFetch } from "./client";
import type { components } from "./schema.gen";

export type SettingsModel = components["schemas"]["SettingsResponse"];
export type AutomationMode = SettingsModel["automation_mode"];

export const DEFAULT_AUTOMATION_MODE: AutomationMode = "evolutive";
const SETTINGS_KEY = ["settings"] as const;

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

export async function saveAutomationMode(mode: AutomationMode): Promise<void> {
  await apiFetch<SettingsModel>("/api/settings", {
    method: "PUT",
    json: { automation_mode: mode },
  });
}

export function useSettings() {
  return useQuery({ queryKey: SETTINGS_KEY, queryFn: fetchSettings });
}
