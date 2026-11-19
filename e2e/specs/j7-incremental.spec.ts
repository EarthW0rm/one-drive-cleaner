// SPDX-License-Identifier: GPL-3.0-only
import { expect, test } from "../fixtures/app";
import { configureCameraRoll, scanAndSettle, seedImages } from "../support/seed";
import { statistic } from "../support/ui";
import { expectNoFailedJobs, waitForSettled, waitForStates } from "../support/waits";

test("J7: varredura seguinte processa só o que mudou e atualiza o item apagado por fora", async ({
  page,
  signedIn: _signedIn,
  api,
  fakeGraph,
}) => {
  const ids = await seedImages(fakeGraph, { photo: 2, screenshot: 2, meme: 1, document: 1 });
  await configureCameraRoll(api);
  await scanAndSettle(api);
  expect((await api.metrics()).by_state).toEqual({ SUGERIDO: 6 });
  const downloadsAfterFirstScan = (await fakeGraph.stats()).cdn.thumb;

  // Fora do app: um arquivo vai para a lixeira do OneDrive e outro chega à Camera Roll.
  await fakeGraph.deleteItem(ids["screenshot-0.jpg"] ?? "");
  await fakeGraph.addImage({ name: "nova-captura.jpg", kind: "screenshot", seed: 99 });

  await page.goto("/varreduras");
  await page.getByRole("button", { name: "Varrer agora" }).click();
  await expect(page.getByText("Varredura iniciada.")).toBeVisible();
  await waitForStates(api, { SUGERIDO: 6, REMOVIDO_EXTERNAMENTE: 1 });
  // O resumo de métricas tem cache de 5 s: com o estado já visível pela API, a tela lê o fresco.
  await page.reload();
  await expect(statistic(page, "Removido externamente")).toHaveText(/1$/);
  await expect(statistic(page, "Sugerido")).toHaveText(/6$/);
  await expect(statistic(page, "Aguardando revisão")).toHaveText(/6$/);

  // RN-07: só a imagem nova foi baixada (a apagada só muda de estado).
  expect((await fakeGraph.stats()).cdn.thumb - downloadsAfterFirstScan).toBe(1);
  expect((await api.metrics()).by_state).toEqual({ SUGERIDO: 6, REMOVIDO_EXTERNAMENTE: 1 });

  // Histórico de mudanças expirado (410): o inventário da pasta é refeito sem perder o que se sabe.
  await fakeGraph.addFaults({ kind: "gone_delta", times: 1 });
  await page.getByRole("button", { name: "Varrer agora" }).click();
  await expect.poll(() => fakeGraph.pendingFaults()).toEqual([]);
  await expect
    .poll(
      async () =>
        (await api.jobs()).filter((job) => job.type === "scan" && job.status === "done").length,
    )
    .toBe(3);
  await waitForSettled(api);
  await expectNoFailedJobs(api);
  expect((await api.metrics()).by_state).toEqual({ SUGERIDO: 6, REMOVIDO_EXTERNAMENTE: 1 });
});
