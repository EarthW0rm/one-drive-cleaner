// SPDX-License-Identifier: GPL-3.0-only
import { expect, test } from "../fixtures/app";
import { configureCameraRoll, labelSuggestions, scanAndSettle, seedImages } from "../support/seed";
import { rowWith, statistic } from "../support/ui";
import { expectNoFailedJobs, waitForQuarantine, waitForSettled } from "../support/waits";

test("J4 Automático: o gate do Evolutivo (>= 200 rótulos, precisão >= 0,98) libera a movimentação (RN-15, RN-16)", async ({
  page,
  signedIn: _signedIn,
  api,
  fakeGraph,
}) => {
  test.setTimeout(120_000);
  // Dataset fake calibrado: o embedder separa as categorias, então a precisão medida é 1,0.
  await seedImages(fakeGraph, { photo: 120, screenshot: 120, meme: 120, document: 120 });
  await configureCameraRoll(api);
  await scanAndSettle(api);

  // Antes do gate: Evolutivo se comporta como Manual e nada se move sozinho.
  await page.goto("/configuracoes");
  await expect(page.getByText("Estado atual do gate")).toBeVisible();
  await expect(rowWith(page, "Elegível ao modo Automático")).toContainText("Não");
  await expect(rowWith(page, "Modo em vigor")).toContainText("Manual");
  expect((await api.quarantine()).total_count).toBe(0);

  // 340 rótulos do utilizador (85 por categoria) numa só requisição: um só retreino.
  const labels = await labelSuggestions(api, {
    real_photo: 85,
    screenshot: 85,
    meme_sticker: 85,
    document_receipt: 85,
  });
  expect(labels.saved).toBe(340);
  expect(labels.train_job_id).not.toBeNull();
  await waitForSettled(api);
  await expectNoFailedJobs(api);

  const { gate } = await api.models();
  expect(gate).toMatchObject({ eligible: true, effective_mode: "auto", labels: 340 });
  expect(gate.precision).toBeGreaterThanOrEqual(0.98);
  expect(gate.support).toBeGreaterThanOrEqual(30);

  // Com o gate aberto, o comportamento Automático move os itens removíveis sem rótulo
  // (confiança >= 0,98): 35 por categoria removível, nenhum de Foto real (RN-09), dentro do
  // teto de 30% do acervo por rodada (RN-27: 144 de 480).
  await waitForQuarantine(api, 105);
  await waitForSettled(api);
  const quarantine = await api.quarantine();
  expect(new Set(quarantine.items.map((item) => item.mode))).toEqual(new Set(["auto"]));
  const ids = await api.categoryIds();
  expect(new Set(quarantine.items.map((item) => item.category_id))).toEqual(
    new Set([ids.screenshot, ids.meme_sticker, ids.document_receipt]),
  );
  expect((await api.metrics()).alerts).toEqual([]);

  await page.reload();
  await expect(rowWith(page, "Elegível ao modo Automático")).toContainText("Sim");
  await expect(rowWith(page, "Modo em vigor")).toContainText("Automático");
  await expect(rowWith(page, "Precisão removível")).toContainText("1,00");

  await page.getByRole("menuitem", { name: "Revisão" }).click();
  await expect(page.getByText("Meta atingida")).toBeVisible();
  await expect(statistic(page, "Rótulos")).toHaveText(/340\s*\/ 200/);

  await page.getByRole("menuitem", { name: "Quarentena" }).click();
  await expect(statistic(page, "Itens")).toHaveText(/105/);
  await expect(page.getByLabel("Itens por categoria")).toContainText("Print de tela: 35");
  await expect(page.getByRole("row", { name: /Automático/ }).first()).toBeVisible();
});
