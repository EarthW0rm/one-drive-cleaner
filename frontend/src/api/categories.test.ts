// SPDX-License-Identifier: GPL-3.0-only
import { QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { createElement, type ReactNode } from "react";
import { describe, expect, it } from "vitest";

import { errorResponse, sessionHandler, TEST_ORIGIN } from "../test/handlers";
import { CATEGORIES } from "../test/review-fixtures";
import { server } from "../test/server";

import {
  CATEGORIES_KEY,
  fetchCategories,
  useCreateCategory,
  useUpdateCategory,
} from "./categories";
import { createQueryClient } from "./queryClient";

const URL_CATEGORIES = `${TEST_ORIGIN}/api/categories`;

function wrapperFor(client: ReturnType<typeof createQueryClient>) {
  function Wrapper({ children }: { children: ReactNode }) {
    return createElement(QueryClientProvider, { client }, children);
  }
  return Wrapper;
}

describe("fetchCategories", () => {
  it("devolve a lista quando a rota existe", async () => {
    server.use(http.get(URL_CATEGORIES, () => HttpResponse.json({ categories: CATEGORIES })));

    await expect(fetchCategories()).resolves.toEqual(CATEGORIES);
  });

  it("devolve null quando a rota ainda não existe (404)", async () => {
    server.use(http.get(URL_CATEGORIES, () => errorResponse("NOT_FOUND", "x", 404)));

    await expect(fetchCategories()).resolves.toBeNull();
  });

  it("propaga os demais erros", async () => {
    server.use(http.get(URL_CATEGORIES, () => errorResponse("INTERNAL", "x", 500)));

    await expect(fetchCategories()).rejects.toMatchObject({ code: "INTERNAL" });
  });
});

describe("useCreateCategory", () => {
  it("envia o corpo com CSRF, devolve a categoria criada e invalida a lista", async () => {
    const bodies: unknown[] = [];
    server.use(
      sessionHandler(),
      http.post(URL_CATEGORIES, async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json(CATEGORIES[1], { status: 201 });
      }),
    );
    const client = createQueryClient(0);
    client.setQueryData(CATEGORIES_KEY, CATEGORIES);
    const { result } = renderHook(() => useCreateCategory(), { wrapper: wrapperFor(client) });

    result.current.mutate({ name: "Print de tela", description: "Capturas", removable: true });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(result.current.data).toEqual(CATEGORIES[1]);
    expect(bodies).toEqual([{ name: "Print de tela", description: "Capturas", removable: true }]);
    expect(client.getQueryState(CATEGORIES_KEY)?.isInvalidated).toBe(true);
  });

  it("propaga o erro 409 de nome duplicado", async () => {
    server.use(
      sessionHandler(),
      http.post(URL_CATEGORIES, () => errorResponse("CONFLICT", "duplicado", 409)),
    );
    const { result } = renderHook(() => useCreateCategory(), {
      wrapper: wrapperFor(createQueryClient(0)),
    });

    result.current.mutate({ name: "Meme", description: "x", removable: true });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
    expect(result.current.error).toMatchObject({ code: "CONFLICT", status: 409 });
  });
});

describe("useUpdateCategory", () => {
  it("envia só o patch para a categoria indicada e invalida a lista", async () => {
    const calls: { path: string; body: unknown }[] = [];
    server.use(
      sessionHandler(),
      http.patch(`${URL_CATEGORIES}/:id`, async ({ request, params }) => {
        calls.push({ path: String(params.id), body: await request.json() });
        return HttpResponse.json({ ...CATEGORIES[1], archived: true });
      }),
    );
    const client = createQueryClient(0);
    client.setQueryData(CATEGORIES_KEY, CATEGORIES);
    const { result } = renderHook(() => useUpdateCategory(), { wrapper: wrapperFor(client) });

    result.current.mutate({ id: 2, patch: { archived: true } });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(calls).toEqual([{ path: "2", body: { archived: true } }]);
    expect(client.getQueryState(CATEGORIES_KEY)?.isInvalidated).toBe(true);
  });

  it("propaga PROTECTED_CATEGORY (422) para categoria protegida", async () => {
    server.use(
      sessionHandler(),
      http.patch(`${URL_CATEGORIES}/:id`, () =>
        errorResponse("PROTECTED_CATEGORY", "protegida", 422),
      ),
    );
    const { result } = renderHook(() => useUpdateCategory(), {
      wrapper: wrapperFor(createQueryClient(0)),
    });

    result.current.mutate({ id: 1, patch: { archived: true } });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
    expect(result.current.error).toMatchObject({ code: "PROTECTED_CATEGORY" });
  });
});
