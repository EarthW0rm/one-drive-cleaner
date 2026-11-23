// SPDX-License-Identifier: GPL-3.0-only
import { http, HttpResponse, type RequestHandler } from "msw";

import type { CategoryModel } from "../api/categories";
import type { ModelsResponse } from "../api/models";
import type { ReviewItem } from "../api/review";

import { TEST_ORIGIN } from "./handlers";
import { metricsHandler, metricsSummary } from "./metrics-fixtures";

const REVIEW_TOTAL = 250;

export const CATEGORIES: readonly CategoryModel[] = [
  {
    id: 1,
    slug: "real_photo",
    description: "Fotos pessoais: nunca vão para a quarentena",
    name: "Foto real",
    removable: false,
    protected: true,
    archived: false,
    labels: 30,
  },
  {
    id: 2,
    slug: "screenshot",
    description: "Capturas de tela",
    name: "Print de tela",
    removable: true,
    protected: false,
    archived: false,
    labels: 12,
  },
  {
    id: 3,
    slug: "meme_sticker",
    description: "Memes e figurinhas",
    name: "Meme",
    removable: true,
    protected: false,
    archived: false,
    labels: 8,
  },
  {
    id: 4,
    slug: "old_doc",
    description: "Documentos digitalizados antigos",
    name: "Documento antigo",
    removable: true,
    protected: false,
    archived: true,
    labels: 0,
  },
];

/** Sugestão cíclica entre as categorias 1..3; confiança cresce com o id (id 1 = mais incerto). */
export function reviewItem(id: number, overrides: Partial<ReviewItem> = {}): ReviewItem {
  return {
    item_id: id,
    suggested_category_id: ((id - 1) % 3) + 1,
    confidence: Math.min(0.99, 0.3 + id / 400),
    user_category_id: null,
    ...overrides,
  };
}

export function models(overrides: Partial<ModelsResponse> = {}): ModelsResponse {
  return {
    current: {
      id: 3,
      strategy: "linear",
      status: "current",
      trigger: "threshold",
      labels_count: 40,
      created_at: "2026-01-01T00:00:00Z",
      metrics: {
        per_category: {
          "1": { precision: 0.97, recall: 0.9, support: 30 },
          "2": { precision: 0.92, recall: 0.8, support: 12 },
        },
      },
    },
    candidates: [],
    auto_eligible: false,
    gate: {
      automation_mode: "evolutive",
      effective_mode: "manual",
      eligible: false,
      has_metrics: true,
      threshold_matches: true,
      precision: 0.93,
      labels: 55,
      support: 12,
      precision_goal: 0.98,
      labels_goal: 100,
      support_goal: 30,
    },
    ...overrides,
  };
}

export interface ReviewApi {
  /** Corpos de `POST /api/labels`, na ordem recebida. */
  readonly labelBodies: { labels: { item_id: number; category_id: number }[] }[];
  /** Corpos de `POST /api/thumbnails`, na ordem recebida. */
  readonly thumbnailBodies: { item_ids: number[]; refresh?: boolean }[];
  /** Consultas de `GET /api/review`, na ordem recebida. */
  readonly reviewQueries: URLSearchParams[];
}

export interface ReviewApiOptions {
  readonly total?: number;
  readonly trainJobId?: number | null;
  readonly categories?: readonly CategoryModel[] | null;
  readonly models?: ModelsResponse;
  /** `labels.since_last_training` do resumo de métricas (padrão 15). */
  readonly labelsSinceTraining?: number;
  /** IDs sem URL de miniatura (o servidor devolve mapa parcial). */
  readonly missingThumbnails?: readonly number[];
}

/**
 * Handlers MSW da Revisão sobre 250 itens sintéticos e URLs fake. Itens rotulados saem da fila
 * (`include_labeled=false`), como na API real; a paginação é por cursor (último `item_id`).
 */
export function reviewApiHandlers(options: ReviewApiOptions = {}): {
  api: ReviewApi;
  handlers: RequestHandler[];
} {
  const total = options.total ?? REVIEW_TOTAL;
  const labeled = new Set<number>();
  const api: ReviewApi = { labelBodies: [], thumbnailBodies: [], reviewQueries: [] };
  const trainJobId = options.trainJobId ?? null;
  const handlers = [
    http.get(`${TEST_ORIGIN}/api/review`, ({ request }) => {
      const query = new URL(request.url).searchParams;
      api.reviewQueries.push(query);
      const limit = Number(query.get("limit") ?? "50");
      const after = Number(query.get("cursor") ?? "0");
      const category = query.get("category_id");
      const ordered = Array.from({ length: total }, (_, index) => reviewItem(index + 1)).filter(
        (item) =>
          !labeled.has(item.item_id) &&
          item.item_id > after &&
          (category === null || item.suggested_category_id === Number(category)),
      );
      const items = ordered.slice(0, limit);
      const last = items.at(-1);
      const next = ordered.length > limit && last !== undefined ? String(last.item_id) : null;
      return HttpResponse.json({ items, next_cursor: next });
    }),
    http.post(`${TEST_ORIGIN}/api/thumbnails`, async ({ request }) => {
      const body = (await request.json()) as ReviewApi["thumbnailBodies"][number];
      api.thumbnailBodies.push(body);
      const urls: Record<string, string> = {};
      for (const id of body.item_ids) {
        if (!(options.missingThumbnails ?? []).includes(id) || body.refresh === true) {
          const version = body.refresh === true ? "fresh" : "v1";
          urls[String(id)] = `https://thumbs.example.test/FAKE-${String(id)}-${version}.jpg`;
        }
      }
      return HttpResponse.json({ urls });
    }),
    http.get(`${TEST_ORIGIN}/api/settings`, () =>
      HttpResponse.json({ retrain_every: 20, automation_mode: "evolutive" }),
    ),
    http.get(`${TEST_ORIGIN}/api/models`, () => HttpResponse.json(options.models ?? models())),
    metricsHandler(
      metricsSummary({
        labels: { total: 55, since_last_training: options.labelsSinceTraining ?? 15 },
      }),
    ),
  ];
  return {
    api,
    handlers: [...handlers, ...labelAndCategoryHandlers(api, labeled, trainJobId, options)],
  };
}

function labelAndCategoryHandlers(
  api: ReviewApi,
  labeled: Set<number>,
  trainJobId: number | null,
  options: ReviewApiOptions,
) {
  return [
    http.post(`${TEST_ORIGIN}/api/labels`, async ({ request }) => {
      const body = (await request.json()) as ReviewApi["labelBodies"][number];
      api.labelBodies.push(body);
      for (const entry of body.labels) {
        labeled.add(entry.item_id);
      }
      return HttpResponse.json({ saved: body.labels.length, train_job_id: trainJobId });
    }),
    http.get(`${TEST_ORIGIN}/api/categories`, () => {
      if (options.categories === null) {
        return HttpResponse.json(
          { error: { code: "NOT_FOUND", message: "Rota ausente", details: null } },
          { status: 404 },
        );
      }
      return HttpResponse.json({ categories: options.categories ?? CATEGORIES });
    }),
  ];
}
