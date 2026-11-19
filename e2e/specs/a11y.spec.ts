// SPDX-License-Identifier: GPL-3.0-only
import { expect, test } from "../fixtures/app";
import { expectNoBlockingA11yViolations } from "../support/a11y";
import {
  configureCameraRoll,
  moveToQuarantine,
  reviewBySlug,
  scanAndSettle,
  seedImages,
} from "../support/seed";
import { itemCheckbox, quarantineRow } from "../support/ui";
import { waitForSettled } from "../support/waits";

test("Acessibilidade: nenhuma violação serious/critical em nenhuma tela (axe)", async ({
  page,
  login,
  api,
  fakeGraph,
}) => {
  test.setTimeout(120_000);
  await seedImages(fakeGraph, { photo: 6, screenshot: 6, meme: 3, document: 3 });

  // Sem sessão: tela de login.
  await page.goto("/login");
  await expect(page.getByRole("heading", { name: "Entrar na conta Microsoft" })).toBeVisible();
  await expectNoBlockingA11yViolations(page, "Login");

  // Primeira configuração: os três passos.
  await login();
  await expect(page.getByRole("heading", { name: "Primeira configuração" })).toBeVisible();
  await expect(page.getByText("Camera Roll", { exact: true })).toBeVisible();
  await expectNoBlockingA11yViolations(page, "Primeira configuração (pastas)");
  await page.getByRole("button", { name: "Avançar" }).click();
  await expect(page.getByRole("radiogroup", { name: "Modo de automação" })).toBeVisible();
  await expectNoBlockingA11yViolations(page, "Primeira configuração (automação)");
  await page.getByRole("button", { name: "Avançar" }).click();
  await expect(page.getByText("Tudo pronto")).toBeVisible();
  await expectNoBlockingA11yViolations(page, "Primeira configuração (concluir)");
  await page.getByRole("button", { name: "Concluir e iniciar varredura" }).click();
  await expect(page).toHaveURL(/\/varreduras$/);

  // Acervo populado: varredura concluída e 3 itens na quarentena.
  await waitForSettled(api);
  const screenshots = (await reviewBySlug(api)).get("screenshot") ?? [];
  const moved = screenshots.slice(0, 3).map((item) => item.item_id);
  await moveToQuarantine(api, moved);
  await expect.poll(async () => (await api.metrics()).by_state).toMatchObject({ QUARENTENA: 3 });

  await page.goto("/varreduras");
  await expect(page.getByRole("heading", { name: "Varreduras" })).toBeVisible();
  await expect(page.getByRole("row", { name: /Varredura/ }).first()).toBeVisible();
  await expectNoBlockingA11yViolations(page, "Varreduras");

  await page.goto("/revisao");
  const queue = await api.reviewQueue();
  await expect(page.getByRole("list", { name: "Itens para revisão" })).toBeVisible();
  await expectNoBlockingA11yViolations(page, "Revisão");
  await itemCheckbox(page, queue[0]?.item_id ?? 0).check();
  await expect(page.getByRole("toolbar", { name: "Ações em lote" })).toBeVisible();
  await expectNoBlockingA11yViolations(page, "Revisão (seleção e ações em lote)");

  await page.goto("/quarentena");
  await expect(quarantineRow(page, moved[0] ?? 0)).toBeVisible();
  await expectNoBlockingA11yViolations(page, "Quarentena");
  await quarantineRow(page, moved[0] ?? 0)
    .getByRole("checkbox")
    .check();
  await page.getByRole("button", { name: "Restaurar" }).click();
  await expect(page.getByRole("dialog", { name: "Restaurar da quarentena" })).toBeVisible();
  await expectNoBlockingA11yViolations(page, "Quarentena (restaurar)", '[role="dialog"]');
  await page.getByRole("button", { name: "Cancelar" }).click();
  await page.getByRole("button", { name: "Excluir" }).click();
  const deletion = page.getByRole("dialog", { name: "Enviar para a lixeira" });
  await expect(deletion.getByText("Itens a excluir")).toBeVisible();
  await expectNoBlockingA11yViolations(page, "Quarentena (excluir)", '[role="dialog"]');
  await deletion.getByRole("button", { name: "Cancelar" }).click();

  await page.goto("/categorias");
  await expect(page.getByRole("heading", { name: "Categorias" })).toBeVisible();
  await expect(page.getByRole("row", { name: /Foto real/ })).toBeVisible();
  await expectNoBlockingA11yViolations(page, "Categorias");
  await page.getByRole("button", { name: "Nova categoria" }).click();
  await expect(page.getByRole("dialog", { name: "Nova categoria" })).toBeVisible();
  await expectNoBlockingA11yViolations(page, "Categorias (nova categoria)", '[role="dialog"]');
  await page.getByRole("button", { name: "Cancelar" }).click();

  await page.goto("/metricas");
  await expect(page.getByRole("img", { name: "Itens por estado", exact: true })).toBeVisible();
  await expectNoBlockingA11yViolations(page, "Métricas");

  await page.goto("/configuracoes");
  await expect(page.getByText("Estado atual do gate")).toBeVisible();
  await expectNoBlockingA11yViolations(page, "Configurações");
});

const DARK_SCREENS = [
  ["/revisao", "Revisão"],
  ["/quarentena", "Quarentena"],
  ["/categorias", "Categorias"],
  ["/varreduras", "Varreduras"],
  ["/metricas", "Métricas"],
  ["/configuracoes", "Configurações"],
] as const;

test("Acessibilidade no tema escuro: nenhuma violação serious/critical nas telas principais (axe)", async ({
  page,
  signedIn: _signedIn,
  api,
  fakeGraph,
}) => {
  test.setTimeout(120_000);
  await page.emulateMedia({ colorScheme: "dark" });
  await seedImages(fakeGraph, { photo: 4, screenshot: 5, meme: 2, document: 2 });
  await configureCameraRoll(api);
  await scanAndSettle(api);
  const screenshots = (await reviewBySlug(api)).get("screenshot") ?? [];
  await moveToQuarantine(
    api,
    screenshots.slice(0, 2).map((item) => item.item_id),
  );

  for (const [path, heading] of DARK_SCREENS) {
    await page.goto(path);
    await expect(page.getByRole("heading", { name: heading, level: 2 })).toBeVisible();
    await expect(page.getByRole("menu", { name: "Navegação principal" })).toBeVisible();
    await expectNoBlockingA11yViolations(page, `${heading} (tema escuro)`);
  }
});
