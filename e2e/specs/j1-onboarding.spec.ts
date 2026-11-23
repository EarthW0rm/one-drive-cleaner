// SPDX-License-Identifier: GPL-3.0-only
import { expect, test } from "../fixtures/app";
import { seedImages } from "../support/seed";
import { expectNoFailedJobs, waitForJob, waitForSettled } from "../support/waits";

test("J1: login, escolha das pastas e do modo, criação das pastas do app e primeira varredura", async ({
  page,
  login,
  api,
  fakeGraph,
}) => {
  await seedImages(fakeGraph, { photo: 2, screenshot: 2 });

  await login();

  // Primeira configuração: a Camera Roll já vem pré-selecionada (J1 passo 3).
  await expect(page).toHaveURL(/\/onboarding$/);
  await expect(page.getByRole("heading", { name: "Primeira configuração" })).toBeVisible();
  await expect(page.getByText("Camera Roll", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Avançar" }).click();

  // O modo de automação padrão é o Evolutivo (J1 passo 4).
  const modes = page.getByRole("radiogroup", { name: "Modo de automação" });
  await expect(modes.getByRole("radio", { name: "Evolutivo" })).toBeChecked();
  await page.getByRole("button", { name: "Avançar" }).click();

  await expect(page.getByText("1 pasta(s) selecionada(s); modo Evolutivo.")).toBeVisible();
  await page.getByRole("button", { name: "Concluir e iniciar varredura" }).click();

  // A primeira varredura começa ao concluir e leva para a tela de Varreduras.
  await expect(page).toHaveURL(/\/varreduras$/);
  await expect(page.getByRole("heading", { name: "Varreduras" })).toBeVisible();
  await waitForJob(api, "scan", "done");
  await waitForSettled(api);
  await expectNoFailedJobs(api);

  // A estrutura de pastas do app existe no OneDrive (J1 passo 5, RN-18).
  expect(await fakeGraph.childNames("OneDrive Cleaner")).toEqual(
    expect.arrayContaining(["Quarentena", "Restaurados"]),
  );
  expect((await api.get<{ automation_mode: string }>("/api/settings")).automation_mode).toBe(
    "evolutive",
  );

  // A primeira configuração não volta a ser pedida.
  await page.goto("/");
  await expect(page).toHaveURL(/\/revisao$/);
  await expect(page.getByRole("heading", { name: "Revisão" })).toBeVisible();
});
