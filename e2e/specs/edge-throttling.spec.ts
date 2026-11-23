// SPDX-License-Identifier: GPL-3.0-only
import { expect, test } from "../fixtures/app";
import { configureCameraRoll, seedImages } from "../support/seed";
import { rowWith, statistic } from "../support/ui";
import { expectNoFailedJobs, waitForSettled, waitForStates } from "../support/waits";

const RETRY_AFTER_S = 1;
const THROTTLED_CALLS = 3;

test("Borda: 429 do OneDrive durante a varredura respeita o Retry-After e o progresso continua", async ({
  page,
  signedIn: _signedIn,
  api,
  fakeGraph,
}) => {
  await seedImages(fakeGraph, { photo: 3, screenshot: 3, meme: 2, document: 2 });
  await configureCameraRoll(api);
  // O OneDrive limita a taxa na consulta de mudanças (2x) e na busca das miniaturas (1x).
  await fakeGraph.addFaults(
    { kind: "throttle", op: "delta", times: 2, retry_after: RETRY_AFTER_S },
    { kind: "throttle", op: "thumbnail_content", times: 1, retry_after: RETRY_AFTER_S },
  );

  await page.goto("/varreduras");
  const startedAt = Date.now();
  await page.getByRole("button", { name: "Varrer agora" }).click();
  await expect(page.getByText("Varredura iniciada.")).toBeVisible();
  // Durante a espera pedida pelo OneDrive o job segue em andamento (nunca falha o lote).
  await expect(rowWith(page, "Varredura")).toContainText("Em andamento");

  await expect.poll(() => fakeGraph.pendingFaults()).toEqual([]);
  await waitForSettled(api);
  await expectNoFailedJobs(api);

  // O ritmo respeitou a espera informada em cada limitação (3 x 1 s).
  expect(Date.now() - startedAt).toBeGreaterThanOrEqual(THROTTLED_CALLS * RETRY_AFTER_S * 900);
  // Nenhum item perdido: todo o acervo foi inventariado, analisado e sugerido.
  await waitForStates(api, { SUGERIDO: 10 });
  await page.reload();
  await expect(rowWith(page, "Varredura")).toContainText("Concluído");
  await expect(statistic(page, "Sugerido")).toHaveText(/10$/);
  await expect(page.getByText("Nenhuma falha de análise")).toBeVisible();
  expect((await api.metrics()).by_state).toEqual({ SUGERIDO: 10 });
});
