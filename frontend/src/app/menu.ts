// SPDX-License-Identifier: GPL-3.0-only
import type { MenuProps } from "antd";

type MenuItems = NonNullable<MenuProps["items"]>;

export const MENU_ITEMS = [
  { key: "/revisao", label: "Revisão" },
  { key: "/quarentena", label: "Quarentena" },
  { key: "/categorias", label: "Categorias" },
  { key: "/varreduras", label: "Varreduras" },
  { key: "/metricas", label: "Métricas" },
  { key: "/configuracoes", label: "Configurações" },
] as const satisfies MenuItems;
