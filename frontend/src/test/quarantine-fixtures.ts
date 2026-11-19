// SPDX-License-Identifier: GPL-3.0-only
import { http, HttpResponse, type RequestHandler } from "msw";

import type {
  DeletionPreview,
  QuarantineItem,
  QuarantineResponse,
  RestoreEntry,
  RestoreNotice,
} from "../api/quarantine";

import { TEST_ORIGIN } from "./handlers";

const ITEM_BYTES = 1_048_576;

/** Item da quarentena: categoria alterna entre 2 (Print de tela) e 3 (Meme) conforme o id. */
export function quarantineItem(
  id: number,
  overrides: Partial<QuarantineItem> = {},
): QuarantineItem {
  return {
    item_id: id,
    category_id: (id % 2) + 2,
    moved_at: "2026-03-05T14:30:00Z",
    mode: "manual",
    size_bytes: ITEM_BYTES,
    ...overrides,
  };
}

export function restoreNotice(id: number, overrides: Partial<RestoreNotice> = {}): RestoreNotice {
  return {
    item_id: id,
    category_id: 1,
    restored_at: "2026-03-06T10:00:00Z",
    restore_reason: "reclassified",
    restore_fallback: false,
    ...overrides,
  };
}

export interface QuarantineApi {
  /** Consultas de `GET /api/quarantine`, na ordem recebida. */
  readonly listQueries: URLSearchParams[];
  readonly restoreBodies: { restores: RestoreEntry[] }[];
  readonly previewBodies: { item_ids: number[] }[];
  readonly deletionBodies: { item_ids: number[]; confirmation_token: string }[];
  readonly moveBodies: { item_ids: number[] }[];
}

export interface QuarantineApiOptions {
  readonly items?: readonly QuarantineItem[];
  readonly notices?: readonly RestoreNotice[];
  /** Tokens devolvidos em sequência pelo preview (o último se repete). */
  readonly tokens?: readonly string[];
  /** `confirmation_token`s que o POST de exclusão rejeita com 422 INVALID_CONFIRMATION. */
  readonly expiredTokens?: readonly string[];
  readonly previewStatus?: number;
  readonly restoreStatus?: number;
  readonly deletionStatus?: number;
  readonly listStatus?: number;
}

function failure(code: string, message: string, status: number) {
  return HttpResponse.json({ error: { code, message, details: null } }, { status });
}

function groupsOf(items: readonly QuarantineItem[]): QuarantineResponse["groups"] {
  const byCategory = new Map<number | null, { count: number; bytes: number }>();
  for (const item of items) {
    const current = byCategory.get(item.category_id) ?? { count: 0, bytes: 0 };
    byCategory.set(item.category_id, {
      count: current.count + 1,
      bytes: current.bytes + item.size_bytes,
    });
  }
  return [...byCategory].map(([category_id, { count, bytes }]) => ({
    category_id,
    count,
    total_bytes: bytes,
  }));
}

/** Handlers MSW da Quarentena: paginação por cursor (id decrescente), grupos e as 3 mutações. */
export function quarantineApiHandlers(options: QuarantineApiOptions = {}): {
  api: QuarantineApi;
  handlers: RequestHandler[];
} {
  const items = options.items ?? Array.from({ length: 3 }, (_, index) => quarantineItem(index + 1));
  const api: QuarantineApi = {
    listQueries: [],
    restoreBodies: [],
    previewBodies: [],
    deletionBodies: [],
    moveBodies: [],
  };
  const tokens = options.tokens ?? ["FAKE-token-1"];
  let previews = 0;
  const handlers = [
    http.get(`${TEST_ORIGIN}/api/quarantine`, ({ request }) => {
      const query = new URL(request.url).searchParams;
      api.listQueries.push(query);
      if (options.listStatus !== undefined) {
        return failure("INTERNAL", "falha", options.listStatus);
      }
      const limit = Number(query.get("limit") ?? "50");
      const before = Number(query.get("cursor") ?? "0");
      const category = query.get("category_id");
      const filtered = [...items]
        .sort((a, b) => b.item_id - a.item_id)
        .filter(
          (item) =>
            (before === 0 || item.item_id < before) &&
            (category === null || item.category_id === Number(category)),
        );
      const page = filtered.slice(0, limit);
      const last = page.at(-1);
      const scope = items.filter(
        (item) => category === null || item.category_id === Number(category),
      );
      const body: QuarantineResponse = {
        items: page,
        total_count: scope.length,
        total_bytes: scope.reduce((sum, item) => sum + item.size_bytes, 0),
        next_cursor: filtered.length > limit && last !== undefined ? String(last.item_id) : null,
        groups: groupsOf(items),
        restore_notices: [...(options.notices ?? [])],
      };
      return HttpResponse.json(body);
    }),
    ...mutationHandlers(api, options, tokens, () => (previews += 1), items),
  ];
  return { api, handlers };
}

function mutationHandlers(
  api: QuarantineApi,
  options: QuarantineApiOptions,
  tokens: readonly string[],
  nextPreview: () => number,
  items: readonly QuarantineItem[],
): RequestHandler[] {
  return [
    http.post(`${TEST_ORIGIN}/api/quarantine/restores`, async ({ request }) => {
      api.restoreBodies.push((await request.json()) as QuarantineApi["restoreBodies"][number]);
      if (options.restoreStatus !== undefined) {
        return failure("INTERNAL", "falha", options.restoreStatus);
      }
      return HttpResponse.json({ job_id: 11 }, { status: 202 });
    }),
    http.post(`${TEST_ORIGIN}/api/quarantine/deletions/preview`, async ({ request }) => {
      const body = (await request.json()) as QuarantineApi["previewBodies"][number];
      api.previewBodies.push(body);
      if (options.previewStatus !== undefined) {
        return failure("NOT_IN_QUARANTINE", "fora", options.previewStatus);
      }
      const index = Math.min(nextPreview() - 1, tokens.length - 1);
      const selected = items.filter((item) => body.item_ids.includes(item.item_id));
      const preview: DeletionPreview = {
        count: body.item_ids.length,
        total_bytes: selected.reduce((sum, item) => sum + item.size_bytes, 0),
        confirmation_token: tokens[index] ?? "FAKE-token",
        expires_at: "2026-03-05T15:00:00+00:00",
      };
      return HttpResponse.json(preview);
    }),
    http.post(`${TEST_ORIGIN}/api/quarantine/deletions`, async ({ request }) => {
      const body = (await request.json()) as QuarantineApi["deletionBodies"][number];
      api.deletionBodies.push(body);
      if (options.expiredTokens?.includes(body.confirmation_token) === true) {
        return failure("INVALID_CONFIRMATION", "expirou", 422);
      }
      if (options.deletionStatus !== undefined) {
        return failure("INTERNAL", "falha", options.deletionStatus);
      }
      return HttpResponse.json({ job_id: 12 }, { status: 202 });
    }),
    http.post(`${TEST_ORIGIN}/api/quarantine/moves`, async ({ request }) => {
      api.moveBodies.push((await request.json()) as QuarantineApi["moveBodies"][number]);
      return HttpResponse.json({ job_id: 13 }, { status: 202 });
    }),
  ];
}
