// SPDX-License-Identifier: GPL-3.0-only
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { errorResponse, sessionHandler, TEST_ORIGIN } from "../test/handlers";
import { server } from "../test/server";

import { fetchSettings, saveAutomationMode } from "./settings";

describe("fetchSettings", () => {
  it("devolve as configurações quando a rota existe", async () => {
    server.use(
      http.get(`${TEST_ORIGIN}/api/settings`, () => HttpResponse.json({ automation_mode: "auto" })),
    );

    await expect(fetchSettings()).resolves.toEqual({ automation_mode: "auto" });
  });

  it("devolve null quando a rota ainda não existe (404)", async () => {
    server.use(http.get(`${TEST_ORIGIN}/api/settings`, () => errorResponse("NOT_FOUND", "x", 404)));

    await expect(fetchSettings()).resolves.toBeNull();
  });

  it("propaga os demais erros", async () => {
    server.use(http.get(`${TEST_ORIGIN}/api/settings`, () => errorResponse("INTERNAL", "x", 500)));

    await expect(fetchSettings()).rejects.toMatchObject({ code: "INTERNAL" });
  });
});

describe("saveAutomationMode", () => {
  it("envia o modo escolhido", async () => {
    let body: unknown;
    server.use(
      sessionHandler(),
      http.put(`${TEST_ORIGIN}/api/settings`, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ automation_mode: "manual" });
      }),
    );

    await saveAutomationMode("manual");

    expect(body).toEqual({ automation_mode: "manual" });
  });
});
