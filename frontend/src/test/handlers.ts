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
