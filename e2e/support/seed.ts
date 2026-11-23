// SPDX-License-Identifier: GPL-3.0-only
import { expect } from "@playwright/test";

import type { FakeGraph } from "../fixtures/app";

import type { AppApi } from "./api";
import type { ImageKind, LabelsResponse, ReviewItem } from "./types";
import { waitForQuarantine, waitForSettled } from "./waits";

/** Quantidade de imagens sintéticas por tipo (cada uma carrega o marcador da categoria). */
export type SeedPlan = Partial<Record<ImageKind, number>>;

/** Tipo de imagem → slug da categoria que o modelo (zero-shot ou treinado) sugere para ele. */
export const KIND_CATEGORY: Readonly<Record<ImageKind, string>> = {
  photo: "real_photo",
  screenshot: "screenshot",
  meme: "meme_sticker",
  document: "document_receipt",
};

const SEED_BATCH = 25;

interface FolderBody {
  readonly folders: readonly { readonly id: string; readonly name: string }[];
}

/** Semeia o OneDrive fake com imagens sintéticas na Camera Roll; devolve o id de cada uma. */
export async function seedImages(
  fakeGraph: FakeGraph,
  plan: SeedPlan,
  prefix = "",
): Promise<Record<string, string>> {
  const jobs: { name: string; kind: ImageKind; seed: number }[] = [];
  for (const [kind, count] of Object.entries(plan) as [ImageKind, number][]) {
    for (let index = 0; index < count; index += 1) {
      jobs.push({ name: `${prefix}${kind}-${String(index)}.jpg`, kind, seed: index });
    }
  }
  const ids: Record<string, string> = {};
  for (let start = 0; start < jobs.length; start += SEED_BATCH) {
    await Promise.all(
      jobs.slice(start, start + SEED_BATCH).map(async (image) => {
        ids[image.name] = await fakeGraph.addImage(image);
      }),
    );
  }
  return ids;
}

/** Id (no OneDrive fake) da pasta em `path` (ex.: `["Pictures", "Camera Roll"]`). */
export async function folderId(api: AppApi, path: readonly string[]): Promise<string> {
  let parent: string | undefined;
  for (const name of path) {
    const query = parent === undefined ? "" : `?parent_id=${encodeURIComponent(parent)}`;
    const body = await api.get<FolderBody>(`/api/folders${query}`);
    const match = body.folders.find((folder) => folder.name === name);
    expect(match, `pasta ${name}`).toBeDefined();
    parent = match?.id;
  }
  if (parent === undefined) {
    throw new Error("caminho vazio");
  }
  return parent;
}

/** Equivale a concluir a primeira configuração: a Camera Roll vira a pasta varrida. */
export async function configureCameraRoll(api: AppApi): Promise<void> {
  const id = await folderId(api, ["Pictures", "Camera Roll"]);
  await api.send("PUT", "/api/scan-roots", { folder_ids: [id] });
}

/** Dispara uma varredura e espera toda a cadeia (extração, classificação, ações) assentar. */
export async function scanAndSettle(api: AppApi, mode: "auto" | "full" = "auto"): Promise<void> {
  await api.send("POST", "/api/scans", { mode });
  await waitForSettled(api);
}

/** Itens da fila de revisão agrupados pela categoria sugerida (slug). */
export async function reviewBySlug(api: AppApi): Promise<Map<string, ReviewItem[]>> {
  const ids = await api.categoryIds();
  const slugById = new Map(Object.entries(ids).map(([slug, id]) => [id, slug]));
  const groups = new Map<string, ReviewItem[]>();
  for (const item of await api.reviewQueue()) {
    const slug = slugById.get(item.suggested_category_id) ?? "?";
    groups.set(slug, [...(groups.get(slug) ?? []), item]);
  }
  return groups;
}

/**
 * Grava, numa única requisição, o rótulo igual à sugestão para os `perCategory` primeiros itens
 * de cada categoria (uma só requisição dispara um só retreino).
 */
export async function labelSuggestions(
  api: AppApi,
  perCategory: Readonly<Record<string, number>>,
): Promise<LabelsResponse> {
  const groups = await reviewBySlug(api);
  const labels = Object.entries(perCategory).flatMap(([slug, count]) =>
    (groups.get(slug) ?? []).slice(0, count).map((item) => ({
      item_id: item.item_id,
      category_id: item.suggested_category_id,
    })),
  );
  return api.send<LabelsResponse>("POST", "/api/labels", { labels });
}

/** Move os itens para a quarentena (como a confirmação do utilizador) e espera terminar. */
export async function moveToQuarantine(api: AppApi, itemIds: readonly number[]): Promise<void> {
  await api.send("POST", "/api/quarantine/moves", { item_ids: itemIds });
  await waitForQuarantine(api, itemIds.length);
  await waitForSettled(api);
}
