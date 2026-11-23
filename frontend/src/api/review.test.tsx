// SPDX-License-Identifier: GPL-3.0-only
import { QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";

import { reviewApiHandlers } from "../test/review-fixtures";
import { server } from "../test/server";

import { createQueryClient } from "./queryClient";
import { reviewPath, useReviewPage, type ReviewQuery } from "./review";

const BASE: ReviewQuery = { categoryId: null, order: "uncertainty", cursor: null, limit: 50 };

describe("reviewPath", () => {
  it("monta só ordem e limite quando não há filtro nem cursor", () => {
    expect(reviewPath(BASE)).toBe("/api/review?order=uncertainty&limit=50");
  });

  it("inclui categoria e cursor quando informados", () => {
    const path = reviewPath({ ...BASE, categoryId: 2, order: "confidence", cursor: "a b" });

    expect(path).toBe("/api/review?order=confidence&limit=50&category_id=2&cursor=a+b");
  });
});

describe("useReviewPage", () => {
  it("carrega a página pedida", async () => {
    const { api, handlers } = reviewApiHandlers();
    server.use(...handlers);
    const client = createQueryClient(0);
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );

    const { result } = renderHook(() => useReviewPage({ ...BASE, limit: 3 }), { wrapper });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(result.current.data?.items.map((item) => item.item_id)).toEqual([1, 2, 3]);
    expect(result.current.data?.next_cursor).toBe("3");
    expect(api.reviewQueries[0]?.get("limit")).toBe("3");
  });
});
