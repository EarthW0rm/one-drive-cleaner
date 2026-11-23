// SPDX-License-Identifier: GPL-3.0-only
import { http, HttpResponse } from "msw";

import type { ErrorCode, SessionResponse } from "../api/client";

export const TEST_ORIGIN = "http://localhost:3000";

export function session(overrides: Partial<SessionResponse> = {}): SessionResponse {
  return {
    account: { display_name: "Maria Teste" },
    auth_state: "authenticated",
    csrf_token: "FAKE-csrf-1",
    ...overrides,
  };
}

export function sessionHandler(overrides: Partial<SessionResponse> = {}) {
  return http.get(`${TEST_ORIGIN}/api/session`, () => HttpResponse.json(session(overrides)));
}

export function errorResponse(code: ErrorCode, message: string, status: number) {
  return HttpResponse.json({ error: { code, message, details: null } }, { status });
}

interface FolderFixture {
  id: string;
  name: string;
  child_count: number;
}

/** Árvore de pastas sintética em 3 níveis (chave `""` = raiz do OneDrive). */
const FOLDER_TREE: Record<string, FolderFixture[]> = {
  "": [
    { id: "FAKE-docs", name: "Documents", child_count: 0 },
    { id: "FAKE-pictures", name: "Pictures", child_count: 1 },
  ],
  "FAKE-pictures": [{ id: "FAKE-camera", name: "Camera Roll", child_count: 1 }],
  "FAKE-camera": [{ id: "FAKE-2025", name: "2025", child_count: 0 }],
};

/** `GET /api/folders` sobre `FOLDER_TREE`; registra cada `parent_id` pedido em `calls`. */
export function foldersHandler(calls: string[] = [], tree = FOLDER_TREE) {
  return http.get(`${TEST_ORIGIN}/api/folders`, ({ request }) => {
    const parent = new URL(request.url).searchParams.get("parent_id") ?? "";
    calls.push(parent);
    return HttpResponse.json({ folders: tree[parent] ?? [] });
  });
}

export function scanRootsHandler(
  roots: { id: number; display_path: string; is_default: boolean }[] = [],
) {
  return http.get(`${TEST_ORIGIN}/api/scan-roots`, () => HttpResponse.json({ scan_roots: roots }));
}

export function jobsHandler(jobs: unknown[] = []) {
  return http.get(`${TEST_ORIGIN}/api/jobs`, () => HttpResponse.json({ jobs }));
}

/** Conta com a primeira configuração concluída: nenhum redirecionamento para /onboarding. */
export function onboardedHandlers() {
  return [
    scanRootsHandler([{ id: 1, display_path: "/Pictures/Docs", is_default: false }]),
    jobsHandler(),
  ];
}
