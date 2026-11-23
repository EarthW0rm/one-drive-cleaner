// SPDX-License-Identifier: GPL-3.0-only
import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { persistTheme, readInitialTheme, systemTheme, THEME_STORAGE_KEY, useTheme } from "./theme";

function mockSystem(dark: boolean): void {
  vi.stubGlobal("matchMedia", vi.fn().mockReturnValue({ matches: dark }));
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("tema", () => {
  it("segue a preferência do sistema sem valor salvo", () => {
    mockSystem(true);

    expect(readInitialTheme()).toBe("dark");
  });

  it("usa claro quando matchMedia não existe", () => {
    vi.stubGlobal("matchMedia", undefined);

    expect(systemTheme()).toBe("light");
  });

  it("prefere o valor salvo", () => {
    mockSystem(true);
    persistTheme("light");

    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("light");
    expect(readInitialTheme()).toBe("light");
  });

  it("ignora valor salvo inválido", () => {
    mockSystem(false);
    window.localStorage.setItem(THEME_STORAGE_KEY, "azul");

    expect(readInitialTheme()).toBe("light");
  });

  it("não quebra com localStorage indisponível", () => {
    mockSystem(true);
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("bloqueado");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("bloqueado");
    });

    expect(readInitialTheme()).toBe("dark");
    expect(() => {
      persistTheme("dark");
    }).not.toThrow();
  });

  it("contexto padrão é claro e o toggle é inofensivo", () => {
    const { result } = renderHook(() => useTheme());

    expect(result.current.mode).toBe("light");
    result.current.toggle();
    expect(result.current.mode).toBe("light");
  });
});
