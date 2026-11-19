// SPDX-License-Identifier: GPL-3.0-only
import type { Page } from "@playwright/test";

import { expect, test } from "../fixtures/app";
import { configureCameraRoll, reviewBySlug, scanAndSettle, seedImages } from "../support/seed";
import { itemCheckbox, reviewCard, statistic, thumbnailOf } from "../support/ui";
import { waitForJob, waitForSettled } from "../support/waits";

async function selectItems(page: Page, itemIds: readonly number[]): Promise<void> {
  for (const itemId of itemIds) {
    await itemCheckbox(page, itemId).check();
  }
}

test("J3: revisão com confirmação e correção, retreino automático ao atingir 20 rótulos (RN-12)", async ({
  page,
  signedIn: _signedIn,
  api,
  fakeGraph,
}) => {
  await seedImages(fakeGraph, { photo: 10, screenshot: 10, meme: 10, document: 10 });
  await configureCameraRoll(api);
  await scanAndSettle(api);
  const groups = await reviewBySlug(api);
  const queue = await api.reviewQueue();
  expect(queue).toHaveLength(40);
  const pick = (slug: string, from: number, to: number): number[] =>
    (groups.get(slug) ?? []).slice(from, to).map((item) => item.item_id);

  await page.goto("/revisao");
  await expect(page.getByRole("heading", { name: "Revisão" })).toBeVisible();
  await expect(page.getByRole("list", { name: "Itens para revisão" })).toBeVisible();
  const first = queue[0];
  expect(first).toBeDefined();
  const firstId = first?.item_id ?? 0;
  await expect(reviewCard(page, firstId)).toBeVisible();
  await expect(reviewCard(page, firstId)).toContainText("100");
  // A miniatura vem do CDN fake e carrega sob a CSP do app.
  const thumbnail = thumbnailOf(page, firstId);
  await expect
    .poll(() => thumbnail.evaluate((img: HTMLImageElement) => img.naturalWidth))
    .toBeGreaterThan(0);
  await expect(statistic(page, "Rótulos")).toHaveText(/0\s*\/ 200/);

  // Lote 1: 10 confirmações da sugestão (5 fotos e 5 prints), ainda abaixo do limiar de 20.
  const batchOne = [...pick("real_photo", 0, 5), ...pick("screenshot", 0, 5)];
  await selectItems(page, batchOne);
  await expect(page.getByText("10 selecionados")).toBeVisible();
  await page.getByRole("button", { name: "Confirmar sugestão" }).click();
  await expect(page.getByText(/10 rótulos salvos\./)).toBeVisible();
  await expect(itemCheckbox(page, batchOne[0] ?? 0)).toHaveCount(0);
  expect((await api.jobs()).filter((job) => job.type === "train")).toEqual([]);

  // Lote 2: 9 confirmações (5 memes e 4 documentos) e 1 correção: um meme que o utilizador
  // classifica como "Documento/recibo" (a classificação dele prevalece sobre a sugestão). O treino exige 5 rótulos por categoria.
  await selectItems(page, [...pick("meme_sticker", 0, 5), ...pick("document_receipt", 0, 4)]);
  await page.getByRole("button", { name: "Confirmar sugestão" }).click();
  await expect(page.getByText(/9 rótulos salvos\./)).toBeVisible();

  const [correctedId] = pick("meme_sticker", 5, 6);
  await selectItems(page, [correctedId ?? 0]);
  await page.getByRole("combobox", { name: "Classificar como" }).click();
  // O antd só expõe as opções reais como texto ("N. Nome"); os `option` do listbox são auxiliares.
  await page.getByText(/^\d\. Documento\/recibo$/).click();

  // O 20º rótulo dispara o retreino em segundo plano e avisa o utilizador (J3 passo 3).
  await expect(page.getByText("Retreino iniciado")).toBeVisible();
  const train = await waitForJob(api, "train", "done");
  expect(train.error_code).toBeNull();
  await waitForSettled(api);
  const models = await api.models();
  expect(models.current).toMatchObject({
    strategy: "logreg",
    trigger: "threshold",
    labels_count: 20,
  });

  // Precisão por categoria e distância até a meta do modo Automático (J3 passo 4).
  await page.reload();
  await expect(page.getByText("Precisão por categoria")).toBeVisible();
  await expect(page.getByText("Meta ainda não atingida")).toBeVisible();
  await expect(statistic(page, "Rótulos")).toHaveText(/20\s*\/ 200/);
  await expect(reviewCard(page, firstId)).toHaveCount(0);
  expect((await api.reviewQueue()).map((item) => item.item_id)).not.toContain(correctedId);
  const ids = await api.categoryIds();
  const labeled = await api.get<{ items: { item_id: number; user_category_id: number | null }[] }>(
    `/api/review?include_labeled=true&limit=100`,
  );
  expect(labeled.items.find((item) => item.item_id === correctedId)?.user_category_id).toBe(
    ids.document_receipt,
  );
});
