// SPDX-License-Identifier: GPL-3.0-only
import { createContext, useContext } from "react";

export type ThemeMode = "light" | "dark";

export const THEME_STORAGE_KEY = "odc.theme";

export interface ThemeContextValue {
  mode: ThemeMode;
  toggle: () => void;
}

export const ThemeContext = createContext<ThemeContextValue>({
  mode: "light",
  toggle: () => undefined,
});

export function useTheme(): ThemeContextValue {
  return useContext(ThemeContext);
}

export function systemTheme(): ThemeMode {
  return typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

/** Preferência salva (`localStorage` protegido) ou a do sistema. */
export function readInitialTheme(): ThemeMode {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    if (stored === "light" || stored === "dark") {
      return stored;
    }
  } catch {
    // localStorage indisponível: usa a preferência do sistema.
  }
  return systemTheme();
}

export function persistTheme(mode: ThemeMode): void {
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, mode);
  } catch {
    // Sem persistência: a preferência vale apenas na sessão atual.
  }
}
