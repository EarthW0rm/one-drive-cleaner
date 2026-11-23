// SPDX-License-Identifier: GPL-3.0-only
import { QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";

import { sessionHandler } from "../test/handlers";
import { reviewApiHandlers } from "../test/review-fixtures";
import { server } from "../test/server";

import { useSaveLabels } from "./labels";
import { MODELS_KEY } from "./models";
import { createQueryClient } from "./queryClient";

describe("useSaveLabels", () => {
  it("envia os rótulos, devolve o resultado e invalida o estado dos modelos", async () => {
    const { api, handlers } = reviewApiHandlers({ trainJobId: 7 });
    server.use(sessionHandler(), ...handlers);
    const client = createQueryClient(0);
    client.setQueryData(MODELS_KEY, { stale: false });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => useSaveLabels(), { wrapper });

    result.current.mutate([{ item_id: 4, category_id: 2 }]);

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(result.current.data).toEqual({ saved: 1, train_job_id: 7 });
    expect(api.labelBodies).toEqual([{ labels: [{ item_id: 4, category_id: 2 }] }]);
    expect(client.getQueryState(MODELS_KEY)?.isInvalidated).toBe(true);
  });
});
