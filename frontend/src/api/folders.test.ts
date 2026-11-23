// SPDX-License-Identifier: GPL-3.0-only
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import {
  errorResponse,
  jobsHandler,
  scanRootsHandler,
  TEST_ORIGIN,
  sessionHandler,
} from "../test/handlers";
import { server } from "../test/server";

import { fetchFolders, fetchNeedsOnboarding, startScan } from "./folders";

describe("fetchFolders", () => {
  it("codifica o parent_id na query", async () => {
    let seen: string | null = null;
    server.use(
      http.get(`${TEST_ORIGIN}/api/folders`, ({ request }) => {
        seen = new URL(request.url).searchParams.get("parent_id");
        return HttpResponse.json({ folders: [] });
      }),
    );

    await fetchFolders("a b!");

    expect(seen).toBe("a b!");
  });
});

describe("fetchNeedsOnboarding", () => {
  it("é verdadeiro sem nenhuma pasta", async () => {
    server.use(scanRootsHandler([]));

    await expect(fetchNeedsOnboarding()).resolves.toBe(true);
  });

  it("é falso quando há pasta escolhida pelo utilizador", async () => {
    server.use(scanRootsHandler([{ id: 2, display_path: "/Docs", is_default: false }]));

    await expect(fetchNeedsOnboarding()).resolves.toBe(false);
  });

  it("com só a Camera Roll padrão depende de já existir uma varredura", async () => {
    server.use(
      scanRootsHandler([{ id: 1, display_path: "/Pictures/Camera Roll", is_default: true }]),
    );
    server.use(jobsHandler([]));
    await expect(fetchNeedsOnboarding()).resolves.toBe(true);

    server.use(jobsHandler([{ id: 1 }]));
    await expect(fetchNeedsOnboarding()).resolves.toBe(false);
  });
});

describe("startScan", () => {
  it("propaga erros diferentes de conflito", async () => {
    server.use(
      sessionHandler(),
      http.post(`${TEST_ORIGIN}/api/scans`, () => errorResponse("INTERNAL", "falha", 500)),
    );

    await expect(startScan()).rejects.toMatchObject({ code: "INTERNAL" });
  });
});
