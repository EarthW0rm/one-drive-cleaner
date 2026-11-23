// SPDX-License-Identifier: GPL-3.0-only
import { describe, expect, it } from "vitest";

import { MENU_ITEMS } from "./menu";

describe("MENU_ITEMS", () => {
  it("lista as 6 telas na ordem definida", () => {
    expect(MENU_ITEMS.map((item) => item.label)).toEqual([
      "Revisão",
      "Quarentena",
      "Categorias",
      "Varreduras",
      "Métricas",
      "Configurações",
    ]);
  });
});
