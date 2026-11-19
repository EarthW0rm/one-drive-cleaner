// SPDX-License-Identifier: GPL-3.0-only
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { errorResponse, TEST_ORIGIN } from "../test/handlers";
import { CATEGORIES } from "../test/review-fixtures";
import { server } from "../test/server";

import { fetchCategories } from "./categories";

const URL_CATEGORIES = `${TEST_ORIGIN}/api/categories`;

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
