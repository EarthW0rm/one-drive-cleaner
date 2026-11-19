// SPDX-License-Identifier: GPL-3.0-only
import { expect, test, FAKE_CDN_ORIGIN } from "../fixtures/app";

const MENU_LABELS = [
  "Revisão",
  "Quarentena",
  "Categorias",
  "Varreduras",
  "Métricas",
  "Configurações",
];

test("abre o app sem sessão, entra com a identidade fake e vê o menu", async ({ page, login }) => {
  await page.goto("/");
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole("heading", { name: "Entrar na conta Microsoft" })).toBeVisible();

  await login();

  const menu = page.getByRole("menu", { name: "Navegação principal" });
  for (const label of MENU_LABELS) {
    await expect(menu.getByRole("menuitem", { name: label })).toBeVisible();
  }
  await expect(page.getByText("Maria Teste")).toBeVisible();
});

test("miniaturas do CDN fake carregam sob a CSP do app", async ({ page, login, fakeGraph }) => {
  // O Graph fake é compartilhado entre os projetos: o nome precisa ser único por navegador.
  const name = `foto-smoke-${test.info().project.name}.jpg`;
  const itemId = await fakeGraph.addImage({ name, kind: "photo" });
  await login();

  const loaded = await page.evaluate(
    (src) =>
      new Promise<boolean>((resolve) => {
        const image = new Image();
        image.onload = () => {
          resolve(image.naturalWidth > 0);
        };
        image.onerror = () => {
          resolve(false);
        };
        image.src = src;
      }),
    `${FAKE_CDN_ORIGIN}/thumb/${itemId}/large?sig=FAKE-SIGNATURE`,
  );

  expect(loaded).toBe(true);
});
