// SPDX-License-Identifier: GPL-3.0-only
import { QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";

import { sessionHandler } from "../test/handlers";
import { quarantineApiHandlers, quarantineItem } from "../test/quarantine-fixtures";
import { server } from "../test/server";

import { MODELS_KEY } from "./models";
import {
  QUARANTINE_KEY,
  quarantinePath,
  useConfirmDeletion,
  useMoveToQuarantine,
  usePreviewDeletion,
  useQuarantine,
  useRestoreFromQuarantine,
} from "./quarantine";
import { createQueryClient } from "./queryClient";
import { REVIEW_KEY } from "./review";

function setup(items = [quarantineItem(1), quarantineItem(2)]) {
  const { api, handlers } = quarantineApiHandlers({ items });
  server.use(sessionHandler(), ...handlers);
  const client = createQueryClient(0);
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return { api, client, wrapper };
}

describe("quarantinePath", () => {
  it("monta a consulta com limite e, quando houver, categoria e cursor", () => {
    expect(quarantinePath({ categoryId: null, cursor: null, limit: 50 })).toBe(
      "/api/quarantine?limit=50",
    );
    expect(quarantinePath({ categoryId: 2, cursor: "9", limit: 25 })).toBe(
      "/api/quarantine?limit=25&category_id=2&cursor=9",
    );
  });
});

describe("useQuarantine", () => {
  it("carrega itens, totais e grupos", async () => {
    const { api, wrapper } = setup();

    const { result } = renderHook(() => useQuarantine({ categoryId: 2, cursor: null, limit: 10 }), {
      wrapper,
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(api.listQueries[0]?.get("category_id")).toBe("2");
    expect(result.current.data?.total_count).toBe(1);
  });
});

describe("useMoveToQuarantine", () => {
  it("envia os itens e invalida a revisão e a quarentena", async () => {
    const { api, client, wrapper } = setup();
    client.setQueryData(REVIEW_KEY, {});
    client.setQueryData(QUARANTINE_KEY, {});
    const { result } = renderHook(() => useMoveToQuarantine(), { wrapper });

    result.current.mutate([1, 2]);

    await waitFor(() => {
      expect(result.current.data).toBe(13);
    });
    expect(api.moveBodies).toEqual([{ item_ids: [1, 2] }]);
    expect(client.getQueryState(REVIEW_KEY)?.isInvalidated).toBe(true);
    expect(client.getQueryState(QUARANTINE_KEY)?.isInvalidated).toBe(true);
  });
});

describe("useRestoreFromQuarantine", () => {
  it("envia restores e invalida a quarentena e o estado dos modelos", async () => {
    const { api, client, wrapper } = setup();
    client.setQueryData(MODELS_KEY, {});
    client.setQueryData(QUARANTINE_KEY, {});
    const { result } = renderHook(() => useRestoreFromQuarantine(), { wrapper });

    result.current.mutate([{ item_id: 1, category_id: 3 }]);

    await waitFor(() => {
      expect(result.current.data).toBe(11);
    });
    expect(api.restoreBodies).toEqual([{ restores: [{ item_id: 1, category_id: 3 }] }]);
    expect(client.getQueryState(MODELS_KEY)?.isInvalidated).toBe(true);
    expect(client.getQueryState(QUARANTINE_KEY)?.isInvalidated).toBe(true);
  });
});

describe("exclusão em duas etapas", () => {
  it("gera o preview e confirma com o token, invalidando a quarentena", async () => {
    const { api, client, wrapper } = setup();
    client.setQueryData(QUARANTINE_KEY, {});
    const { result: previewResult } = renderHook(() => usePreviewDeletion(), { wrapper });
    const { result: confirmResult } = renderHook(() => useConfirmDeletion(), { wrapper });

    previewResult.current.mutate([1, 2]);
    await waitFor(() => {
      expect(previewResult.current.data?.count).toBe(2);
    });
    confirmResult.current.mutate({
      itemIds: [1, 2],
      confirmationToken: previewResult.current.data?.confirmation_token ?? "",
    });

    await waitFor(() => {
      expect(confirmResult.current.data).toBe(12);
    });
    expect(api.deletionBodies).toEqual([{ item_ids: [1, 2], confirmation_token: "FAKE-token-1" }]);
    expect(client.getQueryState(QUARANTINE_KEY)?.isInvalidated).toBe(true);
  });
});
