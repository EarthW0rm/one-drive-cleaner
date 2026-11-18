// SPDX-License-Identifier: GPL-3.0-only
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { errorResponse, session, sessionHandler, TEST_ORIGIN } from "../test/handlers";
import { server } from "../test/server";

import { ApiError, apiFetch, getSession } from "./client";

const URL_ITEMS = `${TEST_ORIGIN}/api/items`;

async function catchError(promise: Promise<unknown>): Promise<ApiError> {
  try {
    await promise;
  } catch (error) {
    if (error instanceof ApiError) {
      return error;
    }
    throw error;
  }
  throw new Error("esperava ApiError");
}

describe("apiFetch", () => {
  it("retorna o JSON de uma requisição GET sem enviar CSRF", async () => {
    let csrf: string | null = "x";
    server.use(
      http.get(URL_ITEMS, ({ request }) => {
        csrf = request.headers.get("X-CSRF-Token");
        return HttpResponse.json({ ok: true });
      }),
    );

    await expect(apiFetch<{ ok: boolean }>("/api/items")).resolves.toEqual({ ok: true });
    expect(csrf).toBeNull();
  });

  it("envia X-CSRF-Token e corpo JSON em POST e memoriza o token", async () => {
    const seen: (string | null)[] = [];
    let sessionCalls = 0;
    server.use(
      http.get(`${TEST_ORIGIN}/api/session`, () => {
        sessionCalls += 1;
        return HttpResponse.json(session());
      }),
      http.post(URL_ITEMS, async ({ request }) => {
        seen.push(request.headers.get("X-CSRF-Token"));
        return HttpResponse.json(await request.json());
      }),
    );

    const first = await apiFetch<{ a: number }>("/api/items", { method: "post", json: { a: 1 } });
    await apiFetch("/api/items", { method: "POST", json: { a: 2 } });

    expect(first).toEqual({ a: 1 });
    expect(seen).toEqual(["FAKE-csrf-1", "FAKE-csrf-1"]);
    expect(sessionCalls).toBe(1);
  });

  it("renova a sessão uma vez em 403 e repete a requisição", async () => {
    const tokens: (string | null)[] = [];
    let sessionCalls = 0;
    server.use(
      http.get(`${TEST_ORIGIN}/api/session`, () => {
        sessionCalls += 1;
        return HttpResponse.json(session({ csrf_token: `FAKE-csrf-${String(sessionCalls)}` }));
      }),
      http.put(URL_ITEMS, ({ request }) => {
        tokens.push(request.headers.get("X-CSRF-Token"));
        return tokens.length === 1
          ? errorResponse("VALIDATION_ERROR", "csrf", 403)
          : new HttpResponse(null, { status: 204 });
      }),
    );

    await expect(apiFetch("/api/items", { method: "PUT" })).resolves.toBeUndefined();
    expect(tokens).toEqual(["FAKE-csrf-1", "FAKE-csrf-2"]);
  });

  it("não repete mais de uma vez quando o 403 persiste", async () => {
    let puts = 0;
    server.use(
      sessionHandler(),
      http.put(URL_ITEMS, () => {
        puts += 1;
        return errorResponse("VALIDATION_ERROR", "csrf", 403);
      }),
    );

    const error = await catchError(apiFetch("/api/items", { method: "PUT" }));

    expect(error.status).toBe(403);
    expect(puts).toBe(2);
  });

  it("converte o erro padrão 422 em ApiError", async () => {
    server.use(http.get(URL_ITEMS, () => errorResponse("VALIDATION_ERROR", "inválido", 422)));

    const error = await catchError(apiFetch("/api/items"));

    expect(error).toMatchObject({ code: "VALIDATION_ERROR", message: "inválido", status: 422 });
  });

  it("converte 5xx sem corpo padrão em INTERNAL", async () => {
    server.use(http.get(URL_ITEMS, () => new HttpResponse("boom", { status: 500 })));

    const error = await catchError(apiFetch("/api/items"));

    expect(error).toMatchObject({ code: "INTERNAL", status: 500 });
  });

  it("usa INTERNAL quando o código do erro é desconhecido", async () => {
    server.use(
      http.get(URL_ITEMS, () =>
        HttpResponse.json({ error: { code: "X", message: "m" } }, { status: 400 }),
      ),
    );

    const error = await catchError(apiFetch("/api/items"));

    expect(error.code).toBe("INTERNAL");
  });

  it("converte falha de rede em NETWORK_ERROR", async () => {
    server.use(http.get(URL_ITEMS, () => HttpResponse.error()));

    const error = await catchError(apiFetch("/api/items"));

    expect(error).toMatchObject({ code: "NETWORK_ERROR", status: 0 });
  });
});

describe("getSession", () => {
  it("retorna a sessão", async () => {
    server.use(sessionHandler({ auth_state: "login_required", account: null }));

    await expect(getSession()).resolves.toMatchObject({ auth_state: "login_required" });
  });
});
