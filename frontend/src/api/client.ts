// SPDX-License-Identifier: GPL-3.0-only
import type { components } from "./schema.gen";

export type ErrorCode = components["schemas"]["ErrorCode"];
export type SessionResponse = components["schemas"]["SessionResponse"];
export type ApiErrorCode = ErrorCode | "NETWORK_ERROR";

const KNOWN_CODES: Record<ErrorCode, true> = {
  VALIDATION_ERROR: true,
  PROTECTED_CATEGORY: true,
  NOT_IN_QUARANTINE: true,
  INVALID_CONFIRMATION: true,
  LOGIN_REQUIRED: true,
  CONFLICT: true,
  GRAPH_UNAVAILABLE: true,
  NOT_FOUND: true,
  INTERNAL: true,
};

const SESSION_PATH = "/api/session";
const CSRF_HEADER = "X-CSRF-Token";
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

export class ApiError extends Error {
  readonly code: ApiErrorCode;
  readonly status: number;
  /** `error.details` do envelope padrão (ex.: `{fields: [{loc, type}]}` no 422). */
  readonly details: unknown;

  constructor(code: ApiErrorCode, message: string, status: number, details: unknown = null) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
    this.details = details;
  }
}

export type ApiInit = Omit<RequestInit, "body"> & { json?: unknown };

let csrfToken: string | null = null;

/** Descarta o token CSRF memorizado (renovação e isolamento entre testes). */
export function resetCsrfToken(): void {
  csrfToken = null;
}

function isKnownCode(value: unknown): value is ErrorCode {
  return typeof value === "string" && value in KNOWN_CODES;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

async function toApiError(response: Response): Promise<ApiError> {
  const body: unknown = await response.json().catch(() => null);
  const error = isRecord(body) ? body.error : undefined;
  if (isRecord(error) && typeof error.message === "string") {
    const code = isKnownCode(error.code) ? error.code : "INTERNAL";
    return new ApiError(code, error.message, response.status, error.details ?? null);
  }
  return new ApiError("INTERNAL", response.statusText, response.status);
}

async function execute(path: string, init: RequestInit): Promise<Response> {
  const url = new URL(path, window.location.origin);
  try {
    return await fetch(url, init);
  } catch {
    throw new ApiError("NETWORK_ERROR", "Falha de rede ao acessar a API local.", 0);
  }
}

async function parse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    throw await toApiError(response);
  }
  const text = await response.text();
  return (text === "" ? undefined : JSON.parse(text)) as T;
}

/** Lê `/api/session` e memoriza o token CSRF. */
export async function getSession(): Promise<SessionResponse> {
  const session = await parse<SessionResponse>(
    await execute(SESSION_PATH, { headers: { Accept: "application/json" } }),
  );
  csrfToken = session.csrf_token;
  return session;
}

async function currentCsrfToken(): Promise<string> {
  csrfToken ??= (await getSession()).csrf_token;
  return csrfToken;
}

async function send(path: string, init: ApiInit, mutating: boolean): Promise<Response> {
  const { json, ...rest } = init;
  const headers = new Headers(rest.headers);
  headers.set("Accept", "application/json");
  const request: RequestInit = { ...rest, headers };
  if (json !== undefined) {
    headers.set("Content-Type", "application/json");
    request.body = JSON.stringify(json);
  }
  if (mutating) {
    headers.set(CSRF_HEADER, await currentCsrfToken());
  }
  return execute(path, request);
}

/**
 * Único ponto de acesso à API. Envia `X-CSRF-Token` em métodos não GET, renova a sessão
 * uma vez em 403 e converte o erro padrão em `ApiError`.
 */
export async function apiFetch<T>(path: string, init: ApiInit = {}): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const mutating = !SAFE_METHODS.has(method);
  let response = await send(path, init, mutating);
  if (mutating && response.status === 403) {
    resetCsrfToken();
    response = await send(path, init, mutating);
  }
  return parse<T>(response);
}
