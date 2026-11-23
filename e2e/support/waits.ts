// SPDX-License-Identifier: GPL-3.0-only
import { expect } from "@playwright/test";

import type { AppApi } from "./api";
import type { Job } from "./types";

/** Rede de segurança das esperas: o que encerra a espera é o estado observado, não o prazo. */
export const WAIT_TIMEOUT_MS = 60_000;
const POLL_INTERVALS_MS = [100, 200, 300, 500];

function isActive(job: Job): boolean {
  return job.status === "queued" || job.status === "running";
}

/**
 * Espera a fila de jobs assentar: nenhum `queued`/`running` e a mesma lista de jobs em duas
 * leituras seguidas (um job encadeado — extração, limpeza, classificação, ações automáticas —
 * entra na fila logo depois do anterior terminar). Jobs `paused` não contam como ativos.
 */
export async function waitForSettled(api: AppApi, timeout = WAIT_TIMEOUT_MS): Promise<void> {
  let previous: string | null = null;
  await expect
    .poll(
      async () => {
        const jobs = await api.jobs();
        const signature = jobs.some(isActive)
          ? null
          : jobs.map((job) => `${String(job.id)}:${job.status}`).join(",");
        const settled = signature !== null && signature !== "" && signature === previous;
        previous = signature;
        return settled;
      },
      { message: "a fila de jobs não assentou", timeout, intervals: POLL_INTERVALS_MS },
    )
    .toBe(true);
}

/** Espera um job do `type` no `status` (via `GET /api/jobs`) e devolve-o. */
export async function waitForJob(
  api: AppApi,
  type: string,
  status: Job["status"],
  timeout = WAIT_TIMEOUT_MS,
): Promise<Job> {
  let found: Job | undefined;
  await expect
    .poll(
      async () => {
        found = (await api.jobs()).find((job) => job.type === type && job.status === status);
        return found !== undefined;
      },
      { message: `job ${type} não chegou a ${status}`, timeout, intervals: POLL_INTERVALS_MS },
    )
    .toBe(true);
  if (found === undefined) {
    throw new Error(`job ${type} não chegou a ${status}`);
  }
  return found;
}

/** Falha se algum job terminou em `failed`. */
export async function expectNoFailedJobs(api: AppApi): Promise<void> {
  const failed = (await api.jobs()).filter((job) => job.status === "failed");
  expect(failed.map((job) => `${job.type}:${job.error_code ?? "?"}`)).toEqual([]);
}

/** Espera o acervo (itens por estado) atingir `expected` (comparação parcial por estado). */
export async function waitForStates(
  api: AppApi,
  expected: Readonly<Record<string, number>>,
  timeout = WAIT_TIMEOUT_MS,
): Promise<void> {
  await expect
    .poll(async () => (await api.metrics()).by_state, {
      message: "contagem de itens por estado",
      timeout,
      intervals: POLL_INTERVALS_MS,
    })
    .toMatchObject(expected);
}

/** Espera a quarentena ter exatamente `count` itens ativos. */
export async function waitForQuarantine(
  api: AppApi,
  count: number,
  timeout = WAIT_TIMEOUT_MS,
): Promise<void> {
  await expect
    .poll(async () => (await api.quarantine()).total_count, {
      message: "itens ativos na quarentena",
      timeout,
      intervals: POLL_INTERVALS_MS,
    })
    .toBe(count);
}
