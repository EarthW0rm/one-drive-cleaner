// SPDX-License-Identifier: GPL-3.0-only
import { describe, expect, it } from "vitest";

import { ApiError } from "./client";
import { createQueryClient, shouldRetry } from "./queryClient";

describe("shouldRetry", () => {
  it.each([
    ["rede", new ApiError("NETWORK_ERROR", "x", 0), true],
    ["5xx", new ApiError("INTERNAL", "x", 503), true],
    ["4xx", new ApiError("VALIDATION_ERROR", "x", 422), false],
    ["erro genérico", new Error("x"), false],
  ])("erro de %s", (_name, error, expected) => {
    expect(shouldRetry(0, error)).toBe(expected);
  });

  it("limita a 2 novas tentativas", () => {
    const error = new ApiError("INTERNAL", "x", 500);

    expect([shouldRetry(0, error), shouldRetry(1, error), shouldRetry(2, error)]).toEqual([
      true,
      true,
      false,
    ]);
  });
});

describe("createQueryClient", () => {
  it("aplica a política de retry e o atraso opcional", () => {
    const options = createQueryClient(0).getDefaultOptions().queries;

    expect(options?.retry).toBe(shouldRetry);
    expect(options?.retryDelay).toBe(0);
    expect(createQueryClient().getDefaultOptions().queries).not.toHaveProperty("retryDelay");
  });
});
