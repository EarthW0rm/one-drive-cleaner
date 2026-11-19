// SPDX-License-Identifier: GPL-3.0-only
import { expect, test } from "../fixtures/app";
import { configureCameraRoll, seedImages } from "../support/seed";
import { expectNoFailedJobs, waitForJob, waitForSettled, waitForStates } from "../support/waits";
import { rowWith, statistic } from "../support/ui";

test("J2: varredura com progresso, pausa e retomada sem perder itens", async ({
  page,
  signedIn: _signedIn,
  api,
  fakeGraph,
}) => {
  await seedImages(fakeGraph, { photo: 4, screenshot: 4, meme: 2, document: 2 });
  await configureCameraRoll(api);
  // A primeira consulta de mudanças é limitada pelo OneDrive: a varredura fica em espera e dá
  // tempo de pausá-la pela interface; a pausa vale já para o estado do job e o trabalho cancelado recomeça do ponto salvo.
  await fakeGraph.addFaults({ kind: "throttle", op: "delta", times: 1, retry_after: 5 });

  await page.goto("/varreduras");
  await expect(page.getByText("Nenhuma varredura executada")).toBeVisible();
  await page.getByRole("button", { name: "Varrer agora" }).click();
  await expect(page.getByText("Varredura iniciada.")).toBeVisible();

  // Só pausa depois que o OneDrive devolveu o 429 (o job está dentro da espera).
  await expect.poll(() => fakeGraph.pendingFaults()).toEqual([]);
  await page.getByRole("button", { name: "Pausar" }).click();
  await expect(rowWith(page, "Varredura").getByText("Pausado")).toBeVisible();
  await expect(page.getByText("Pausado por você.")).toBeVisible();
  await expect(page.getByText("Nenhum item", { exact: false })).toHaveCount(0);

  await page.getByRole("button", { name: "Retomar" }).click();
  await waitForJob(api, "scan", "done");
  await waitForSettled(api);

  // Resultado: acervo pré-categorizado e pronto para revisão (J2 passo 6). O resumo de métricas
  // tem cache de 5 s no servidor: a espera pelo estado também garante a leitura fresca da tela.
  await waitForStates(api, { SUGERIDO: 12 });
  await page.reload();
  await expect(rowWith(page, "Varredura").getByText("Concluído")).toBeVisible();
  await expect(page.getByText("1 de 1")).toBeVisible();
  await expect(statistic(page, "Sugerido")).toHaveText(/12$/);
  await expect(statistic(page, "Aguardando extração")).toHaveText(/0$/);
  await expect(statistic(page, "Aguardando revisão")).toHaveText(/12$/);
  await expectNoFailedJobs(api);
  expect((await api.metrics()).by_state).toEqual({ SUGERIDO: 12 });
});
