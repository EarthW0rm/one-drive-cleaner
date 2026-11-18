// SPDX-License-Identifier: GPL-3.0-only
import { describe, expect, it } from "vitest";

import { ApiError } from "../api/client";

import { ERROR_MESSAGES, errorMessage } from "./errors";

describe("errorMessage", () => {
  it("mapeia o código da API para PT-BR", () => {
    expect(errorMessage(new ApiError("LOGIN_REQUIRED", "x", 401))).toBe(
      ERROR_MESSAGES.LOGIN_REQUIRED,
    );
  });

  it("usa a mensagem interna para erros desconhecidos", () => {
    expect(errorMessage(new Error("x"))).toBe(ERROR_MESSAGES.INTERNAL);
  });
});
