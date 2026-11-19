// SPDX-License-Identifier: GPL-3.0-only
import type { Locator, Page } from "@playwright/test";

/**
 * Bloco de um `Statistic` (título + valor) a partir do título: o antd não dá papel ARIA ao
 * conjunto, então sobe do texto do título ao contêiner do componente (classe pública
 * `ant-statistic`).
 */
export function statistic(scope: Page | Locator, title: string): Locator {
  return scope
    .getByText(title, { exact: true })
    .locator(
      "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' ant-statistic ')][1]",
    );
}

/** Linha da tabela (papel `row`) que contém o texto. */
export function rowWith(scope: Page | Locator, text: string | RegExp): Locator {
  return scope.getByRole("row").filter({ hasText: text });
}

/** Cartão da grade de revisão do item (rótulo acessível `Item N: sugestão ...`). */
export function reviewCard(page: Page, itemId: number): Locator {
  return page.getByRole("group", { name: new RegExp(`^Item ${String(itemId)}: `) });
}

/** Caixa de seleção do item na grade de revisão. */
export function itemCheckbox(page: Page, itemId: number): Locator {
  return page.getByRole("checkbox", { name: `Selecionar item ${String(itemId)}`, exact: true });
}

/** Miniatura do item (a grade de revisão e a quarentena usam o mesmo texto alternativo). */
export function thumbnailOf(page: Page, itemId: number): Locator {
  return page.getByRole("img", { name: `Miniatura do item ${String(itemId)}`, exact: true });
}

/** Linha da tabela da Quarentena do item (identificada pela miniatura). */
export function quarantineRow(page: Page, itemId: number): Locator {
  return page.getByRole("row").filter({ has: thumbnailOf(page, itemId) });
}
