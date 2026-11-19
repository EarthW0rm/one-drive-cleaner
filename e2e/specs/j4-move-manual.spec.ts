// SPDX-License-Identifier: GPL-3.0-only
import { expect, test } from "../fixtures/app";
import { configureCameraRoll, reviewBySlug, scanAndSettle, seedImages } from "../support/seed";
import { itemCheckbox } from "../support/ui";
import { expectNoFailedJobs, waitForQuarantine, waitForSettled } from "../support/waits";

const CAMERA_ROLL = "Pictures/Camera Roll";
const QUARANTINE = "OneDrive Cleaner/Quarentena";

test("J4 manual: move os itens confirmados e bloqueia Foto real (RN-09)", async ({
  page,
  signedIn: _signedIn,
  api,
  fakeGraph,
}) => {
  const ids = await seedImages(fakeGraph, { photo: 3, screenshot: 4, meme: 2, document: 1 });
  const allNames = Object.keys(ids).sort();
  await configureCameraRoll(api);
  await scanAndSettle(api);
  // Modo Evolutivo com o gate fechado se comporta como Manual: nada se move sozinho (J4 passo 3).
  expect((await api.quarantine()).total_count).toBe(0);
  const groups = await reviewBySlug(api);
  const photos = (groups.get("real_photo") ?? []).map((item) => item.item_id);
  const screenshots = (groups.get("screenshot") ?? []).map((item) => item.item_id);
  const memes = (groups.get("meme_sticker") ?? []).map((item) => item.item_id);

  await page.goto("/revisao");
  const toolbar = page.getByRole("toolbar", { name: "Ações em lote" });

  // RN-09: item de Foto real nunca vai para a quarentena, nem pela interface nem pela API.
  await itemCheckbox(page, photos[0] ?? 0).check();
  await expect(toolbar.getByRole("button", { name: "Mover para quarentena" })).toBeDisabled();
  await expect(toolbar.getByRole("status")).toContainText("Foto real (categoria protegida)");
  const refused = await api.attempt("POST", "/api/quarantine/moves", { item_ids: [photos[0]] });
  expect(refused.status).toBe(422);
  expect(refused.body).toMatchObject({ error: { code: "PROTECTED_CATEGORY" } });
  expect((await api.quarantine()).total_count).toBe(0);
  await toolbar.getByRole("button", { name: "Limpar seleção" }).click();

  // Seleção removível: dois prints e um meme, com confirmação explícita.
  const chosen = [screenshots[0] ?? 0, screenshots[1] ?? 0, memes[0] ?? 0];
  for (const itemId of chosen) {
    await itemCheckbox(page, itemId).check();
  }
  await expect(page.getByText("3 selecionados")).toBeVisible();
  await toolbar.getByRole("button", { name: "Mover para quarentena" }).click();
  const dialog = page.getByRole("dialog", { name: "Mover 3 itens para a quarentena?" });
  await expect(dialog).toBeVisible();
  expect((await api.quarantine()).total_count).toBe(0);
  await dialog.getByRole("button", { name: "Mover para a quarentena" }).click();
  await expect(page.getByText(/3 itens enfileirados para a quarentena\./)).toBeVisible();

  await waitForQuarantine(api, 3);
  await waitForSettled(api);
  await expectNoFailedJobs(api);

  // No OneDrive: os 3 arquivos estão na Quarentena, nenhum duplicado ou perdido (RN-18).
  const quarantined = (await fakeGraph.childNames(QUARANTINE)) ?? [];
  const remaining = (await fakeGraph.childNames(CAMERA_ROLL)) ?? [];
  expect(quarantined).toHaveLength(3);
  expect([...quarantined, ...remaining].sort()).toEqual(allNames);
  expect(quarantined.every((name) => !name.startsWith("photo-"))).toBe(true);

  // A tela de Quarentena mostra os 3 itens, agrupados por categoria, no modo Manual.
  await page.getByRole("menuitem", { name: "Quarentena" }).click();
  await expect(page.getByRole("heading", { name: "Quarentena" })).toBeVisible();
  const itemsByCategory = page.getByLabel("Itens por categoria");
  await expect(itemsByCategory).toContainText("Print de tela: 2");
  await expect(itemsByCategory).toContainText("Meme/figurinha: 1");
  await expect(page.getByRole("row", { name: /Manual/ })).toHaveCount(3);
});
