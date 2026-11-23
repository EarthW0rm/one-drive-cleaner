// SPDX-License-Identifier: GPL-3.0-only
import { expect, test } from "../fixtures/app";
import {
  configureCameraRoll,
  moveToQuarantine,
  reviewBySlug,
  scanAndSettle,
  seedImages,
} from "../support/seed";
import { quarantineRow } from "../support/ui";
import { expectNoFailedJobs, waitForSettled, waitForStates } from "../support/waits";

const CAMERA_ROLL = "Pictures/Camera Roll";
const QUARANTINE = "OneDrive Cleaner/Quarentena";

test("J5: restaurar falso positivo devolve o item à origem e vira aprendizado (RN-11)", async ({
  page,
  signedIn: _signedIn,
  api,
  fakeGraph,
}) => {
  const ids = await seedImages(fakeGraph, { photo: 2, screenshot: 6, meme: 2, document: 2 });
  const allNames = Object.keys(ids).sort();
  await configureCameraRoll(api);
  await scanAndSettle(api);
  const screenshots = (await reviewBySlug(api)).get("screenshot") ?? [];
  const moved = screenshots.slice(0, 3).map((item) => item.item_id);
  await moveToQuarantine(api, moved);
  const quarantinedBefore = (await fakeGraph.childNames(QUARANTINE)) ?? [];
  expect(quarantinedBefore).toHaveLength(3);

  await page.goto("/quarentena");
  await expect(page.getByRole("heading", { name: "Quarentena" })).toBeVisible();
  const target = moved[0] ?? 0;
  await quarantineRow(page, target).getByRole("checkbox").check();
  await expect(page.getByText("1 selecionado")).toBeVisible();
  await page.getByRole("button", { name: "Restaurar" }).click();

  // O utilizador informa a categoria correta: o item era uma foto real.
  const dialog = page.getByRole("dialog", { name: "Restaurar da quarentena" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("combobox", { name: "Categoria correta" }).click();
  await page.getByTitle("Foto real").click();
  await dialog.getByRole("button", { name: "Restaurar" }).click();
  await expect(page.getByText("1 item enfileirado para restauração.")).toBeVisible();

  await waitForStates(api, { RESTAURADO: 1, QUARENTENA: 2 });
  await waitForSettled(api);
  await expectNoFailedJobs(api);

  // Voltou à pasta e ao nome originais; os outros dois seguem na quarentena (RN-19).
  const quarantinedAfter = (await fakeGraph.childNames(QUARANTINE)) ?? [];
  const restored = quarantinedBefore.filter((name) => !quarantinedAfter.includes(name));
  expect(restored).toHaveLength(1);
  const remaining = (await fakeGraph.childNames(CAMERA_ROLL)) ?? [];
  expect(remaining).toContain(restored[0]);
  expect([...quarantinedAfter, ...remaining].sort()).toEqual(allNames);
  expect((await api.quarantine()).total_count).toBe(2);

  // A correção é registrada como rótulo do utilizador (RN-11) e a lista reflete a restauração.
  const categories = await api.categoryIds();
  const labeled = await api.get<{ items: { item_id: number; user_category_id: number | null }[] }>(
    "/api/review?include_labeled=true&limit=100",
  );
  expect(labeled.items.find((item) => item.item_id === target)?.user_category_id).toBe(
    categories.real_photo,
  );
  await page.reload();
  await expect(quarantineRow(page, target)).toHaveCount(0);
  await expect(page.getByRole("row", { name: /Print de tela/ })).toHaveCount(2);
});
