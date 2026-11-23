// SPDX-License-Identifier: GPL-3.0-only
import { expect, test as base, type APIRequestContext } from "@playwright/test";

import { AppApi } from "../support/api";
import { FAKE_AUTH_CODE, FAKE_CDN_ORIGIN, FAKE_IDP_HOST } from "../support/constants";
import { startStack } from "../support/launcher";
import type { ImageKind } from "../support/types";

export { FAKE_CDN_ORIGIN, FAKE_IDP_HOST, FAKE_AUTH_CODE };
export type { ImageKind };

/** Falha do Graph fake (`tests/fakes/faults.py`): `kind` e `times` mais os campos do tipo. */
export interface FaultSpec {
  readonly kind: string;
  readonly times?: number;
  readonly [field: string]: string | number | null | undefined;
}

export interface AppUrls {
  readonly appUrl: string;
  readonly graphUrl: string;
}

export interface GraphStats {
  readonly calls: Readonly<Record<string, number>>;
  readonly cdn: { readonly thumb: number; readonly content: number };
}

export interface NewImage {
  readonly name: string;
  readonly kind?: ImageKind;
  readonly parentId?: string;
  readonly seed?: number;
}

/** Controle do Graph fake pelos endpoints `/__fake__` (o estado vive no processo do launcher). */
export interface FakeGraph {
  seed: (count: number, seed?: number) => Promise<void>;
  addImage: (image: NewImage) => Promise<string>;
  deleteItem: (itemId: string) => Promise<void>;
  addFaults: (...faults: FaultSpec[]) => Promise<void>;
  clearFaults: () => Promise<void>;
  /** Chamadas recebidas por operação e downloads do CDN fake (`GET /__fake__/stats`). */
  stats: () => Promise<GraphStats>;
  /** Faltas ainda pendentes (`times` restantes): vazio quando todas já dispararam. */
  pendingFaults: () => Promise<FaultSpec[]>;
  /** Id do item (arquivo ou pasta) no caminho `Pictures/Camera Roll/x.jpg`; `null` se não existe. */
  idAt: (path: string) => Promise<string | null>;
  /** Nomes dos filhos da pasta; `null` se a pasta não existe. */
  childNames: (path: string) => Promise<string[] | null>;
  /** Pasta pai (id) e nome atuais do item; `null` se foi apagado ou não existe. */
  locationOf: (itemId: string) => Promise<{ parentId: string; name: string } | null>;
}

interface Fixtures {
  app: AppUrls;
  fakeGraph: FakeGraph;
  /** Entra pela interface (o teste começa sem sessão) e aguarda o menu principal. */
  login: () => Promise<void>;
  /** Cliente tipado da API local, na sessão do navegador do teste (preparação e observação). */
  api: AppApi;
  /** Entra pela API (sem interface), para os cenários que não são sobre o login. */
  signedIn: undefined;
  /** Serve o CDN fake (https, sem DNS) a partir do Graph fake, sem afrouxar a CSP do app. */
  fakeCdn: undefined;
}

async function control(
  request: APIRequestContext,
  url: string,
  method: "post" | "delete",
  data?: object,
): Promise<void> {
  const response =
    method === "post" ? await request.post(url, { data }) : await request.delete(url);
  expect(response.ok(), `${method.toUpperCase()} ${url}`).toBe(true);
}

/** Qualquer `Bearer` serve ao Graph fake; o observador não consome falhas de token. */
const OBSERVER = { authorization: "Bearer FAKE-e2e-observer" };

function createFakeGraph(request: APIRequestContext, graphUrl: string): FakeGraph {
  const base = `${graphUrl}/__fake__`;
  const api = `${graphUrl}/v1.0`;
  return {
    seed: (count, seed = 0) => control(request, `${base}/populate`, "post", { count, seed }),
    addImage: async ({ name, kind = "photo", parentId, seed = 0 }) => {
      const body = { name, kind, seed, ...(parentId === undefined ? {} : { parent_id: parentId }) };
      const response = await request.post(`${base}/images`, { data: body });
      expect(response.ok(), "POST /__fake__/images").toBe(true);
      return ((await response.json()) as { id: string }).id;
    },
    deleteItem: (itemId) =>
      control(request, `${base}/items/${encodeURIComponent(itemId)}`, "delete"),
    addFaults: (...faults) => control(request, `${base}/faults`, "post", faults),
    clearFaults: () => control(request, `${base}/faults`, "delete"),
    stats: async () => {
      const response = await request.get(`${base}/stats`);
      expect(response.ok(), "GET /__fake__/stats").toBe(true);
      return (await response.json()) as GraphStats;
    },
    pendingFaults: async () => {
      const response = await request.get(`${base}/faults`);
      expect(response.ok(), "GET /__fake__/faults").toBe(true);
      return (await response.json()) as FaultSpec[];
    },
    idAt: async (path) => {
      const response = await request.get(`${api}/me/drive/root:/${encodeURI(path)}`, {
        headers: OBSERVER,
      });
      return response.ok() ? ((await response.json()) as { id: string }).id : null;
    },
    childNames: async (path) => {
      const folder = await request.get(`${api}/me/drive/root:/${encodeURI(path)}`, {
        headers: OBSERVER,
      });
      if (!folder.ok()) {
        return null;
      }
      const { id } = (await folder.json()) as { id: string };
      const children = await request.get(`${api}/me/drive/items/${id}/children?$top=999`, {
        headers: OBSERVER,
      });
      expect(children.ok(), `GET children de ${path}`).toBe(true);
      const body = (await children.json()) as { value: { name: string }[] };
      return body.value.map((child) => child.name);
    },
    locationOf: async (itemId) => {
      const response = await request.get(`${api}/me/drive/items/${encodeURIComponent(itemId)}`, {
        headers: OBSERVER,
      });
      if (!response.ok()) {
        return null;
      }
      const body = (await response.json()) as {
        name: string;
        deleted?: object;
        parentReference?: { id: string };
      };
      return body.deleted === undefined && body.parentReference !== undefined
        ? { parentId: body.parentReference.id, name: body.name }
        : null;
    },
  };
}

export const test = base.extend<Fixtures>({
  // Uma instância isolada (app, banco, Graph fake, identidade e modelo) por teste.
  app: async ({}, use, testInfo) => {
    const stack = await startStack();
    try {
      await use({ appUrl: stack.appUrl, graphUrl: stack.graphUrl });
    } finally {
      if (testInfo.status !== testInfo.expectedStatus) {
        await testInfo.attach("launcher-stderr", { body: stack.log(), contentType: "text/plain" });
      }
      await stack.stop();
    }
  },

  baseURL: async ({ app }, use) => {
    await use(app.appUrl);
  },

  fakeGraph: async ({ app, playwright }, use) => {
    const request = await playwright.request.newContext();
    await use(createFakeGraph(request, app.graphUrl));
    await request.dispose();
  },

  api: async ({ context, app }, use) => {
    await use(new AppApi(context.request, app.appUrl));
  },

  signedIn: async ({ api }, use) => {
    await api.login();
    await use(undefined);
  },

  fakeCdn: [
    async ({ context, app }, use) => {
      await context.route(`${FAKE_CDN_ORIGIN}/**`, async (route) => {
        const source = new URL(route.request().url());
        const upstream = `${app.graphUrl}/__cdn__${source.pathname}${source.search}`;
        await route.fulfill({ response: await context.request.get(upstream) });
      });
      await use(undefined);
      // Miniaturas ainda em voo ao fim do teste não podem virar erro de rota.
      await context.unrouteAll({ behavior: "ignoreErrors" });
    },
    { auto: true },
  ],

  login: async ({ page, app }, use) => {
    await use(async () => {
      // O provedor fake é um host `https` inexistente e o navegador não roteia o salto de
      // redirect até ele: a rota executa `/auth/login` no servidor (sem seguir o redirect), lê o
      // `state` da URL do provedor e responde direto com um salto ao callback do app.
      await page.context().route(`${app.appUrl}/auth/login`, async (route) => {
        const upstream = await route.fetch({ maxRedirects: 0 });
        const location = new URL(upstream.headers().location ?? "");
        expect(location.hostname, "redirect do login").toBe(FAKE_IDP_HOST);
        const callback = new URL("/auth/callback", app.appUrl);
        callback.searchParams.set("code", FAKE_AUTH_CODE);
        callback.searchParams.set("state", location.searchParams.get("state") ?? "");
        // WebKit não aceita `fulfill` com status de redirect: um meta refresh faz o mesmo salto.
        const target = callback.toString().replaceAll("&", "&amp;");
        await route.fulfill({
          contentType: "text/html",
          body: `<!doctype html><meta http-equiv="refresh" content="0;url=${target}">`,
        });
      });
      await page.goto("/login");
      await page.getByRole("link", { name: "Entrar com conta Microsoft" }).click();
      await expect(page.getByRole("menu", { name: "Navegação principal" })).toBeVisible();
    });
  },
});

export { expect };
