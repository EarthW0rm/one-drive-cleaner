// SPDX-License-Identifier: GPL-3.0-only
import { expect, test } from "../fixtures/app";
import {
  configureCameraRoll,
  moveToQuarantine,
  reviewBySlug,
  scanAndSettle,
  seedImages,
} from "../support/seed";
import { quarantineRow, statistic } from "../support/ui";
import { expectNoFailedJobs, waitForSettled, waitForStates } from "../support/waits";

const QUARANTINE = "OneDrive Cleaner/Quarentena";

test("J5: excluir exige confirmação com quantidade e volume e envia para a lixeira (RN-20)", async ({
  page,
  signedIn: _signedIn,
  api,
  fakeGraph,
}) => {
  const ids = await seedImages(fakeGraph, { photo: 1, screenshot: 4 });
  await configureCameraRoll(api);
  await scanAndSettle(api);
  const screenshots = (await reviewBySlug(api)).get("screenshot") ?? [];
  const moved = screenshots.slice(0, 3).map((item) => item.item_id);
  await moveToQuarantine(api, moved);
  const quarantinedBefore = (await fakeGraph.childNames(QUARANTINE)) ?? [];
  expect(quarantinedBefore).toHaveLength(3);

  // Sem a confirmação emitida pela tela (token de uso único), a API recusa a exclusão.
  const refused = await api.attempt("POST", "/api/quarantine/deletions", {
    item_ids: moved,
    confirmation_token: "FAKE-token-inexistente",
  });
  expect(refused.status).toBe(422);
  expect(refused.body).toMatchObject({ error: { code: "INVALID_CONFIRMATION" } });
  expect((await api.quarantine()).total_count).toBe(3);

  await page.goto("/quarentena");
  const doomed = moved.slice(0, 2);
  for (const itemId of doomed) {
    await quarantineRow(page, itemId).getByRole("checkbox").check();
  }
  await expect(page.getByText("2 selecionados")).toBeVisible();
  await page.getByRole("button", { name: "Excluir" }).click();

  // A confirmação mostra a quantidade e o volume e só habilita com a quantidade digitada.
  const dialog = page.getByRole("dialog", { name: "Enviar para a lixeira" });
  await expect(statistic(dialog, "Itens a excluir")).toHaveText(/2$/);
  await expect(statistic(dialog, "Volume a excluir")).toContainText(/KB|B/);
  const confirm = dialog.getByRole("button", { name: "Enviar para a lixeira" });
  const typed = dialog.getByRole("textbox", {
    name: "Digite a quantidade de itens para confirmar",
  });
  await expect(confirm).toBeDisabled();
  await typed.fill("3");
  await expect(confirm).toBeDisabled();
  await typed.fill("2");
  await expect(confirm).toBeEnabled();
  expect((await api.quarantine()).total_count).toBe(3);
  await confirm.click();
  await expect(page.getByText(/2 itens seguem para a lixeira do OneDrive/)).toBeVisible();

  await waitForStates(api, { EXCLUIDO: 2, QUARENTENA: 1 });
  await waitForSettled(api);
  await expectNoFailedJobs(api);

  // No OneDrive: os dois arquivos saíram da Quarentena rumo à lixeira (nunca exclusão
  // permanente: o item continua existindo, marcado como apagado) e o terceiro segue lá.
  const remaining = (await fakeGraph.childNames(QUARANTINE)) ?? [];
  expect(remaining).toHaveLength(1);
  const deleted = quarantinedBefore.filter((name) => !remaining.includes(name));
  expect(deleted).toHaveLength(2);
  for (const name of deleted) {
    expect(await fakeGraph.locationOf(ids[name] ?? "")).toBeNull();
  }
  expect((await api.quarantine()).total_count).toBe(1);

  await page.reload();
  await expect(statistic(page, "Itens")).toHaveText(/1$/);
  for (const itemId of doomed) {
    await expect(quarantineRow(page, itemId)).toHaveCount(0);
  }
});
