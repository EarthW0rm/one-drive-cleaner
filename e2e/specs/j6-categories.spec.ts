// SPDX-License-Identifier: GPL-3.0-only
import { expect, test } from "../fixtures/app";
import { configureCameraRoll, reviewBySlug, scanAndSettle, seedImages } from "../support/seed";
import { reviewCard, rowWith } from "../support/ui";
import { expectNoFailedJobs, waitForJob, waitForSettled } from "../support/waits";

const NEW_NAME = "Cartão de visita";

test("J6: cria categoria, classifica exemplos, vê a sugestão após o retreino e renomeia/arquiva", async ({
  page,
  signedIn: _signedIn,
  api,
  fakeGraph,
}) => {
  await seedImages(fakeGraph, { document: 24, photo: 8 });
  await configureCameraRoll(api);
  await scanAndSettle(api);
  const groups = await reviewBySlug(api);
  const documents = (groups.get("document_receipt") ?? []).map((item) => item.item_id);
  const photos = (groups.get("real_photo") ?? []).map((item) => item.item_id);
  expect(documents).toHaveLength(24);

  await page.goto("/categorias");
  await expect(page.getByRole("heading", { name: "Categorias" })).toBeVisible();
  // Foto real é protegida: não pode ser renomeada nem arquivada (RN-09).
  await expect(page.getByRole("button", { name: "Editar Foto real" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Arquivar Foto real" })).toBeDisabled();

  // Nova categoria removível, com nome e descrição.
  await page.getByRole("button", { name: "Nova categoria" }).click();
  const drawer = page.getByRole("dialog", { name: "Nova categoria" });
  await drawer.getByRole("textbox", { name: "Nome" }).fill(NEW_NAME);
  await drawer
    .getByRole("textbox", { name: "Descrição" })
    .fill("Cartão de visita de pessoa ou empresa");
  await expect(drawer.getByRole("switch", { name: "Removível" })).toBeChecked();
  await drawer.getByRole("button", { name: "Salvar" }).click();
  await expect(page.getByText("Categoria criada.")).toBeVisible();
  const row = rowWith(page, NEW_NAME);
  await expect(row).toContainText("Ativa");
  await expect(row).toContainText("Sim");

  // Nome repetido é recusado.
  await page.getByRole("button", { name: "Nova categoria" }).click();
  await drawer.getByRole("textbox", { name: "Nome" }).fill(NEW_NAME);
  await drawer.getByRole("textbox", { name: "Descrição" }).fill("Outra descrição");
  await drawer.getByRole("button", { name: "Salvar" }).click();
  await expect(drawer.getByText("Já existe uma categoria com este nome.")).toBeVisible();
  await drawer.getByRole("button", { name: "Cancelar" }).click();

  // O utilizador classifica 12 exemplos na nova categoria (e 8 fotos reais) e o retreino
  // (20 rótulos) passa a sugeri-la para as imagens parecidas.
  const created = (await api.categories()).find((category) => category.name === NEW_NAME)?.id;
  expect(created).toBeDefined();
  const ids = await api.categoryIds();
  const labels = [
    ...documents.slice(0, 12).map((itemId) => ({ item_id: itemId, category_id: created })),
    ...photos.map((itemId) => ({ item_id: itemId, category_id: ids.real_photo })),
  ];
  const saved = await api.send<{ train_job_id: number | null }>("POST", "/api/labels", { labels });
  expect(saved.train_job_id).not.toBeNull();
  await waitForJob(api, "train", "done");
  await waitForSettled(api);
  await expectNoFailedJobs(api);

  await page.goto("/revisao");
  const remaining = documents[12] ?? 0;
  await expect(reviewCard(page, remaining)).toHaveAccessibleName(
    new RegExp(`sugestão ${NEW_NAME}`),
  );
  const suggested = (await api.reviewQueue()).filter((item) => documents.includes(item.item_id));
  expect(suggested).toHaveLength(12);
  expect(new Set(suggested.map((item) => item.suggested_category_id))).toEqual(new Set([created]));

  // Renomear e arquivar (J6 passo 3).
  await page.goto("/categorias");
  await page.getByRole("button", { name: `Editar ${NEW_NAME}` }).click();
  const editor = page.getByRole("dialog", { name: "Editar categoria" });
  await editor.getByRole("textbox", { name: "Nome" }).fill("Cartão profissional");
  await editor.getByRole("button", { name: "Salvar" }).click();
  await expect(page.getByText("Categoria atualizada.")).toBeVisible();
  await expect(rowWith(page, "Cartão profissional")).toBeVisible();

  await page.getByRole("button", { name: "Arquivar Meme/figurinha" }).click();
  await page.getByRole("button", { name: "Arquivar", exact: true }).last().click();
  await expect(page.getByText('Categoria "Meme/figurinha" arquivada.')).toBeVisible();
  await expect(rowWith(page, "Meme/figurinha")).toContainText("Arquivada");
});
