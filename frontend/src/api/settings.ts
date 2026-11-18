// SPDX-License-Identifier: GPL-3.0-only
import { useQuery } from "@tanstack/react-query";

import { ApiError, apiFetch } from "./client";

export type AutomationMode = "manual" | "auto" | "evolutive";

export const DEFAULT_AUTOMATION_MODE: AutomationMode = "evolutive";
const SETTINGS_KEY = ["settings"] as const;

/** Contrato do plan 4.1 (`GET|PUT /api/settings`); a rota entra na TF-156-01. */
export interface SettingsModel {
  readonly automation_mode: AutomationMode;
}

/**
 * Lê as configurações. Rota ausente (404) devolve `null`: o assistente oculta o passo de
 * automação até a TF-156-01 expor `/api/settings`.
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
