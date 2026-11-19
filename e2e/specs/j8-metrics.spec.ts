// SPDX-License-Identifier: GPL-3.0-only
import { expect, test } from "../fixtures/app";
import { configureCameraRoll, labelSuggestions, scanAndSettle, seedImages } from "../support/seed";
import { statistic } from "../support/ui";
import {
  expectNoFailedJobs,
  waitForJob,
  waitForQuarantine,
  waitForSettled,
} from "../support/waits";

const MOVED_BY_CAP = 93;
const RESTORED = 5;

test("J8: tela de Métricas com acervo, quarentena, falsos positivos, disco e alertas", async ({
  page,
  signedIn: _signedIn,
  api,
  fakeGraph,
}) => {
  test.setTimeout(120_000);
  await seedImages(fakeGraph, { photo: 10, screenshot: 100, meme: 100, document: 100 });
  await configureCameraRoll(api);
  await scanAndSettle(api);
  // 211 rótulos abrem o gate; sobram 99 itens removíveis sem rótulo, mas a rodada automática só
  // pode mover 30% do acervo (310 -> 93, RN-27): o excedente fica para revisão e gera alerta.
  await labelSuggestions(api, {
    real_photo: 10,
    screenshot: 67,
    meme_sticker: 67,
    document_receipt: 67,
  });
  await waitForQuarantine(api, MOVED_BY_CAP);
  await waitForSettled(api);
  await expectNoFailedJobs(api);

  // Restaurar itens movidos por engano (falsos positivos) pesa na taxa dos últimos 7 dias.
  const ids = await api.categoryIds();
  const moved = (await api.quarantine()).items.slice(0, RESTORED);
  await api.send("POST", "/api/quarantine/restores", {
    restores: moved.map((item) => ({ item_id: item.item_id, category_id: ids.real_photo })),
  });
  await waitForJob(api, "restore", "done");
  await waitForSettled(api);
  await expectNoFailedJobs(api);
  // A nova rodada automática (após a reclassificação) leva os 6 excedentes que o teto deixou
  // de fora; os 5 restaurados pelo utilizador nunca voltam sozinhos: 93 - 5 + 6.
  await waitForQuarantine(api, 94);

  // O resumo tem cache de 5 s: esperar os alertas pela API garante a leitura fresca na tela.
  await expect
    .poll(async () => (await api.metrics()).alerts.map((alert) => alert.code).sort())
    .toEqual(["AUTO_MOVE_CAPPED", "HIGH_FALSE_POSITIVE"]);
  await page.goto("/metricas");
  await expect(page.getByRole("heading", { name: "Métricas" })).toBeVisible();

  // Alertas (J8 passo 1).
  await expect(
    page.getByText("A movimentação automática atingiu o teto por rodada", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByText("Muitos itens movidos foram restaurados por você nos últimos 7 dias", {
      exact: false,
    }),
  ).toBeVisible();

  // Acervo por estado, quarentena, espaço liberável e taxa de falso positivo.
  await expect(statistic(page, "Itens no acervo")).toHaveText(/310$/);
  await expect(statistic(page, "Itens em quarentena")).toHaveText(/94$/);
  await expect(statistic(page, "Volume liberável")).toHaveText(/\d+(,\d+)? (KB|MB)$/);
  const rate = (await api.metrics()).false_positive_rate.last_7d;
  expect(rate).toBeGreaterThan(0.02);
  const percent = new Intl.NumberFormat("pt-BR", { style: "percent", maximumFractionDigits: 1 });
  await expect(statistic(page, "Falsos positivos (7 dias)")).toContainText(
    percent.format(rate ?? 0),
  );

  // Gráficos (acervo por estado, quarentena por categoria, precisão por versão e vigente) e disco.
  for (const name of [
    "Itens por estado",
    "Quarentena por categoria",
    "Precisão removível por versão do modelo",
    "Precisão do modelo vigente contra a meta",
  ]) {
    const chart = page.getByRole("img", { name, exact: true });
    await expect(chart).toBeVisible();
    await expect(chart.locator("canvas, svg").first()).toBeVisible();
  }
  await expect(
    page.getByRole("progressbar", { name: "Uso de disco contra o orçamento" }),
  ).toBeVisible();
  await expect(page.getByText("Temporários", { exact: false })).toBeVisible();
});
