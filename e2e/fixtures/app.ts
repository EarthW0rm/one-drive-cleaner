// SPDX-License-Identifier: GPL-3.0-only
import { expect, test as base, type APIRequestContext } from "@playwright/test";

/** Origem das URLs de miniatura que o Graph fake devolve (`GraphServer(cdn_url=...)` padrão). */
export const FAKE_CDN_ORIGIN = "https://cdn.graph-fake.example.invalid";
/** Host do provedor de identidade fake (`tests/fakes/identity_fake.py`). */
export const FAKE_IDP_HOST = "login.fake.invalid";
/** Código de autorização aceito pela identidade fake. */
export const FAKE_AUTH_CODE = "FAKE-code";

export type ImageKind = "photo" | "screenshot" | "meme" | "document";

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
}

interface SessionBody {
  readonly csrf_token: string;
  readonly auth_state: "authenticated" | "login_required";
}

interface Fixtures {
  app: AppUrls;
  fakeGraph: FakeGraph;
  /** Entra pela interface (o teste começa sem sessão) e aguarda o menu principal. */
  login: () => Promise<void>;
  /** Garante que cada teste comece sem sessão (o app e o Graph fake são compartilhados). */
  loggedOut: undefined;
  /** Serve o CDN fake (https, sem DNS) a partir do Graph fake, sem afrouxar a CSP do app. */
  fakeCdn: undefined;
}

function requiredEnv(name: string): string {
  const value = process.env[name];
  if (value === undefined || value === "") {
    throw new Error(`${name} ausente: o launcher E2E não informou as URLs (webServer).`);
  }
  return value;
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

function createFakeGraph(request: APIRequestContext, graphUrl: string): FakeGraph {
  const base = `${graphUrl}/__fake__`;
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
  };
}

async function logoutIfAuthenticated(request: APIRequestContext, appUrl: string): Promise<void> {
  const session = await request.get(`${appUrl}/api/session`);
  const body = (await session.json()) as SessionBody;
  if (body.auth_state === "authenticated") {
    const response = await request.post(`${appUrl}/auth/logout`, {
      headers: { "x-csrf-token": body.csrf_token },
    });
    expect(response.status(), "POST /auth/logout").toBe(204);
  }
}

export const test = base.extend<Fixtures>({
  app: async ({}, use) => {
    await use({
      appUrl: requiredEnv("ODC_E2E_APP_URL"),
      graphUrl: requiredEnv("ODC_E2E_GRAPH_URL"),
    });
  },

  baseURL: async ({ app }, use) => {
    await use(app.appUrl);
  },

  fakeGraph: async ({ app, playwright }, use) => {
    const request = await playwright.request.newContext();
    await use(createFakeGraph(request, app.graphUrl));
    // Falhas pendentes de um teste não podem vazar para o seguinte.
    await control(request, `${app.graphUrl}/__fake__/faults`, "delete");
    await request.dispose();
  },

  loggedOut: [
    async ({ context, app }, use) => {
      await logoutIfAuthenticated(context.request, app.appUrl);
      await use(undefined);
    },
    { auto: true },
  ],

  fakeCdn: [
    async ({ context, app }, use) => {
      await context.route(`${FAKE_CDN_ORIGIN}/**`, async (route) => {
        const source = new URL(route.request().url());
        const upstream = `${app.graphUrl}/__cdn__${source.pathname}${source.search}`;
        await route.fulfill({ response: await context.request.get(upstream) });
      });
      await use(undefined);
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
