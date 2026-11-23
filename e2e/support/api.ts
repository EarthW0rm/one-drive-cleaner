// SPDX-License-Identifier: GPL-3.0-only
import { expect, type APIRequestContext } from "@playwright/test";

import { FAKE_AUTH_CODE, FAKE_IDP_HOST } from "./constants";
import type {
  CategoriesResponse,
  Category,
  JobsResponse,
  Job,
  MetricsSummary,
  ModelsResponse,
  QuarantineResponse,
  ReviewItem,
  ReviewResponse,
} from "./types";

interface SessionBody {
  readonly csrf_token: string;
  readonly auth_state: "authenticated" | "login_required";
}

type IdMap = Readonly<Record<string, number>>;

/**
 * Cliente tipado da API local, na mesma sessão (cookies) do navegador do teste: serve para
 * preparar o estado e para observar o resultado sem depender de rótulos locais da interface.
 */
export class AppApi {
  constructor(
    private readonly request: APIRequestContext,
    private readonly appUrl: string,
  ) {}

  /** Entra pela API (sem interface): segue o fluxo OAuth com a identidade fake. */
  async login(): Promise<void> {
    const authorize = await this.request.get(`${this.appUrl}/auth/login`, { maxRedirects: 0 });
    const location = new URL(authorize.headers().location ?? "");
    expect(location.hostname, "redirect do login").toBe(FAKE_IDP_HOST);
    const callback = await this.request.get(`${this.appUrl}/auth/callback`, {
      maxRedirects: 0,
      params: { code: FAKE_AUTH_CODE, state: location.searchParams.get("state") ?? "" },
    });
    expect(callback.status(), "GET /auth/callback").toBe(302);
  }

  async get<T>(path: string): Promise<T> {
    const response = await this.request.get(`${this.appUrl}${path}`);
    expect(response.ok(), `GET ${path}`).toBe(true);
    return (await response.json()) as T;
  }

  /** Mutação com o token CSRF da sessão; devolve o corpo (ou `undefined` sem conteúdo). */
  async send<T = undefined>(
    method: "POST" | "PUT" | "PATCH" | "DELETE",
    path: string,
    data?: object,
  ): Promise<T> {
    const session = await this.get<SessionBody>("/api/session");
    const response = await this.request.fetch(`${this.appUrl}${path}`, {
      method,
      headers: { "x-csrf-token": session.csrf_token },
      ...(data === undefined ? {} : { data }),
    });
    expect(response.ok(), `${method} ${path}: ${await response.text()}`).toBe(true);
    const text = await response.text();
    return (text === "" ? undefined : JSON.parse(text)) as T;
  }

  /** Como `send`, mas devolve o status e o corpo em vez de exigir sucesso (para as recusas). */
  async attempt(
    method: "POST" | "PUT" | "PATCH" | "DELETE",
    path: string,
    data?: object,
  ): Promise<{ status: number; body: unknown }> {
    const session = await this.get<SessionBody>("/api/session");
    const response = await this.request.fetch(`${this.appUrl}${path}`, {
      method,
      headers: { "x-csrf-token": session.csrf_token },
      ...(data === undefined ? {} : { data }),
    });
    const text = await response.text();
    return {
      status: response.status(),
      body: text === "" ? undefined : (JSON.parse(text) as unknown),
    };
  }

  jobs(): Promise<Job[]> {
    return this.get<JobsResponse>("/api/jobs?limit=100").then((body) => body.jobs);
  }

  metrics(): Promise<MetricsSummary> {
    return this.get<MetricsSummary>("/api/metrics/summary");
  }

  models(): Promise<ModelsResponse> {
    return this.get<ModelsResponse>("/api/models");
  }

  quarantine(): Promise<QuarantineResponse> {
    return this.get<QuarantineResponse>("/api/quarantine?limit=100");
  }

  async categories(): Promise<Category[]> {
    return (await this.get<CategoriesResponse>("/api/categories")).categories;
  }

  /** `slug` → id das categorias (`screenshot`, `real_photo`, `meme_sticker`, `document_receipt`). */
  async categoryIds(): Promise<IdMap> {
    return Object.fromEntries(
      (await this.categories()).map((category) => [category.slug, category.id]),
    );
  }

  /** Toda a fila de revisão (itens ainda sem rótulo), página a página. */
  async reviewQueue(): Promise<ReviewItem[]> {
    const items: ReviewItem[] = [];
    let cursor: string | null = null;
    do {
      const query: string = cursor === null ? "" : `&cursor=${encodeURIComponent(cursor)}`;
      const page: ReviewResponse = await this.get<ReviewResponse>(`/api/review?limit=100${query}`);
      items.push(...page.items);
      cursor = page.next_cursor;
    } while (cursor !== null);
    return items;
  }
}
