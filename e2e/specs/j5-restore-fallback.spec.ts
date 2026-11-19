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

const QUARANTINE = "OneDrive Cleaner/Quarentena";
const RESTORED = "OneDrive Cleaner/Restaurados";

test("J5: com a pasta original apagada e irrecriável, restaura em Restaurados e avisa (RN-19)", async ({
  page,
  signedIn: _signedIn,
  api,
  fakeGraph,
}) => {
  await seedImages(fakeGraph, { photo: 1, screenshot: 4, meme: 1 });
  await configureCameraRoll(api);
  await scanAndSettle(api);
  const screenshots = (await reviewBySlug(api)).get("screenshot") ?? [];
  const moved = screenshots.slice(0, 2).map((item) => item.item_id);
  await moveToQuarantine(api, moved);
  const quarantinedBefore = (await fakeGraph.childNames(QUARANTINE)) ?? [];

  // Fora do app: a Camera Roll é apagada e um arquivo com o mesmo nome toma o lugar da pasta,
  // então recriar o caminho original é impossível.
  const picturesId = await fakeGraph.idAt("Pictures");
  const cameraRollId = await fakeGraph.idAt("Pictures/Camera Roll");
  expect(picturesId).not.toBeNull();
  expect(cameraRollId).not.toBeNull();
  await fakeGraph.deleteItem(cameraRollId ?? "");
  await fakeGraph.addImage({ name: "Camera Roll", kind: "photo", parentId: picturesId ?? "" });

  await page.goto("/quarentena");
  const target = moved[0] ?? 0;
  await quarantineRow(page, target).getByRole("checkbox").check();
  await page.getByRole("button", { name: "Restaurar" }).click();
  const dialog = page.getByRole("dialog", { name: "Restaurar da quarentena" });
  await dialog.getByRole("combobox", { name: "Categoria correta" }).click();
  await page.getByTitle("Print de tela").click();
  await dialog.getByRole("button", { name: "Restaurar" }).click();
  await expect(page.getByText("1 item enfileirado para restauração.")).toBeVisible();

  await waitForStates(api, { RESTAURADO: 1, QUARENTENA: 1 });
  await waitForSettled(api);
  await expectNoFailedJobs(api);

  // O item foi para `Restaurados` (nome original) e a interface avisa do desvio.
  const restoredNames = (await fakeGraph.childNames(RESTORED)) ?? [];
  expect(restoredNames).toHaveLength(1);
  expect(quarantinedBefore).toContain(restoredNames[0]);
  expect(await fakeGraph.childNames(QUARANTINE)).not.toContain(restoredNames[0]);
  const notices = (await api.quarantine()).restore_notices;
  expect(notices).toHaveLength(1);
  expect(notices[0]).toMatchObject({ item_id: target, restore_fallback: true });

  await page.reload();
  const warning = page.getByRole("alert").filter({ hasText: "restaurado na pasta Restaurados" });
  await expect(warning).toBeVisible();
  await expect(warning).toContainText("A pasta original não estava disponível");
  await expect(warning).toContainText(`Item ${String(target)}`);
  await expect(quarantineRow(page, target)).toHaveCount(0);
});
