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
import { expectNoFailedJobs, waitForJob, waitForSettled, waitForStates } from "../support/waits";

const CAMERA_ROLL = "Pictures/Camera Roll";
const QUARANTINE = "OneDrive Cleaner/Quarentena";

test("RN-17: após o retreino, item da quarentena reclassificado para categoria não removível volta à origem", async ({
  page,
  signedIn: _signedIn,
  api,
  fakeGraph,
}) => {
  const ids = await seedImages(fakeGraph, { screenshot: 30, document: 12 });
  const allNames = Object.keys(ids).sort();
  await configureCameraRoll(api);
  await scanAndSettle(api);
  const groups = await reviewBySlug(api);
  const screenshots = (groups.get("screenshot") ?? []).map((item) => item.item_id);
  const documents = (groups.get("document_receipt") ?? []).map((item) => item.item_id);
  const categories = await api.categoryIds();

  // 5 prints vão para a quarentena (modo Manual, por confirmação do utilizador).
  const quarantined = screenshots.slice(0, 5);
  await moveToQuarantine(api, quarantined);
  await page.goto("/quarentena");
  await expect(page.getByText("A quarentena está vazia")).toHaveCount(0);
  await expect(page.getByRole("row", { name: /Print de tela/ })).toHaveCount(5);

  // O utilizador passa a classificar prints parecidos como "Foto real" (20 de 20 rótulos novos
  // e mais 10 documentos): o retreino aprende que esse tipo de imagem é foto real.
  const labels = [
    ...screenshots
      .slice(5, 25)
      .map((itemId) => ({ item_id: itemId, category_id: categories.real_photo })),
    ...documents
      .slice(0, 10)
      .map((itemId) => ({ item_id: itemId, category_id: categories.document_receipt })),
  ];
  const saved = await api.send<{ saved: number; train_job_id: number | null }>(
    "POST",
    "/api/labels",
    { labels },
  );
  expect(saved.train_job_id).not.toBeNull();
  await waitForJob(api, "train", "done");
  await waitForJob(api, "restore", "done");
  await waitForSettled(api);
  await expectNoFailedJobs(api);

  // Os 5 itens saíram da quarentena e voltaram à Camera Roll, sem duplicar nem perder arquivos;
  // restaurados, voltam à fila de revisão (a reclassificação os sugere de novo).
  await waitForStates(api, { SUGERIDO: 42 });
  expect((await api.metrics()).by_state).toEqual({ SUGERIDO: 42 });
  expect((await api.quarantine()).total_count).toBe(0);
  expect(await fakeGraph.childNames(QUARANTINE)).toEqual([]);
  expect((await fakeGraph.childNames(CAMERA_ROLL))?.slice().sort()).toEqual(allNames);
  const notices = (await api.quarantine()).restore_notices;
  expect(notices.map((notice) => notice.item_id).sort()).toEqual([...quarantined].sort());
  expect(new Set(notices.map((notice) => notice.restore_reason))).toEqual(
    new Set(["reclassified"]),
  );
  expect(notices.every((notice) => notice.category_id === categories.real_photo)).toBe(true);

  // A interface lista os itens como restaurados automaticamente (aviso da RN-17).
  await page.reload();
  const info = page
    .getByRole("alert")
    .filter({ hasText: "restaurados automaticamente após o retreino" });
  await expect(info).toContainText("5 itens foram restaurados automaticamente");
  await expect(info).toContainText("Foto real");
  await expect(page.getByText("A quarentena está vazia")).toBeVisible();
  for (const itemId of quarantined) {
    await expect(quarantineRow(page, itemId)).toHaveCount(0);
  }
});
