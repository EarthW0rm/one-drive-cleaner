// SPDX-License-Identifier: GPL-3.0-only
import { QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { useState, type ReactNode } from "react";
import { describe, expect, it } from "vitest";

import { createQueryClient } from "../../api/queryClient";
import { errorResponse, sessionHandler, TEST_ORIGIN } from "../../test/handlers";
import { reviewApiHandlers } from "../../test/review-fixtures";
import { server } from "../../test/server";

import { useThumbnails } from "./useThumbnails";

const URL_THUMBS = `${TEST_ORIGIN}/api/thumbnails`;

function Wrapper({ children }: Readonly<{ children: ReactNode }>): ReactNode {
  const [client] = useState(() => createQueryClient(0));
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function setup(ids: readonly number[], options: Parameters<typeof reviewApiHandlers>[0] = {}) {
  const { api, handlers } = reviewApiHandlers(options);
  server.use(sessionHandler(), ...handlers);
  return { api, hook: renderHook(() => useThumbnails(ids), { wrapper: Wrapper }) };
}

describe("useThumbnails", () => {
  it("busca as URLs da página em uma única chamada", async () => {
    const { api, hook } = setup([1, 2, 3]);

    expect(hook.result.current.isLoading).toBe(true);
    await waitFor(() => {
      expect(hook.result.current.isLoading).toBe(false);
    });

    expect(hook.result.current.urls["2"]).toBe("https://thumbs.example.test/FAKE-2-v1.jpg");
    expect(api.thumbnailBodies).toEqual([{ item_ids: [1, 2, 3], refresh: false }]);
  });

  it("não chama a API sem itens", () => {
    const { api, hook } = setup([]);

    expect(hook.result.current.isLoading).toBe(false);
    expect(api.thumbnailBodies).toEqual([]);
  });

  it("refaz uma vez as URLs ausentes da resposta", async () => {
    const { api, hook } = setup([1, 2], { missingThumbnails: [2] });

    await waitFor(() => {
      expect(hook.result.current.urls["2"]).toBe("https://thumbs.example.test/FAKE-2-fresh.jpg");
    });

    expect(api.thumbnailBodies).toEqual([
      { item_ids: [1, 2], refresh: false },
      { item_ids: [2], refresh: true },
    ]);
  });

  it("mantém o mapa parcial quando a nova tentativa das ausentes falha", async () => {
    let calls = 0;
    server.use(
      sessionHandler(),
      http.post(URL_THUMBS, () => {
        calls += 1;
        return calls === 1
          ? HttpResponse.json({ urls: { "1": "https://thumbs.example.test/FAKE-1.jpg" } })
          : errorResponse("GRAPH_UNAVAILABLE", "x", 502);
      }),
    );

    const { result } = renderHook(() => useThumbnails([1, 2]), { wrapper: Wrapper });

    await waitFor(() => {
      expect(result.current.isLoading).toBe(false);
    });
    expect(result.current.urls).toEqual({ "1": "https://thumbs.example.test/FAKE-1.jpg" });
    expect(result.current.isError).toBe(false);
  });

  it("sinaliza erro quando a primeira chamada falha", async () => {
    server.use(
      sessionHandler(),
      http.post(URL_THUMBS, () => errorResponse("INTERNAL", "x", 400)),
    );

    const { result } = renderHook(() => useThumbnails([1]), { wrapper: Wrapper });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
  });

  it("troca a URL com refresh no primeiro erro de imagem e desiste no segundo", async () => {
    const { api, hook } = setup([1]);
    await waitFor(() => {
      expect(hook.result.current.urls["1"]).toBeDefined();
    });

    act(() => {
      hook.result.current.onImageError(1);
    });
    await waitFor(() => {
      expect(hook.result.current.urls["1"]).toBe("https://thumbs.example.test/FAKE-1-fresh.jpg");
    });
    expect(hook.result.current.failed.has(1)).toBe(false);

    act(() => {
      hook.result.current.onImageError(1);
    });

    expect(hook.result.current.failed.has(1)).toBe(true);
    expect(api.thumbnailBodies.filter((body) => body.refresh === true)).toHaveLength(1);
  });

  it("marca como falha quando a nova URL não vem", async () => {
    let calls = 0;
    server.use(
      sessionHandler(),
      http.post(URL_THUMBS, () => {
        calls += 1;
        return HttpResponse.json({
          urls: calls === 1 ? { "1": "https://thumbs.example.test/FAKE-1.jpg" } : {},
        });
      }),
    );
    const { result } = renderHook(() => useThumbnails([1]), { wrapper: Wrapper });
    await waitFor(() => {
      expect(result.current.urls["1"]).toBeDefined();
    });

    act(() => {
      result.current.onImageError(1);
    });

    await waitFor(() => {
      expect(result.current.failed.has(1)).toBe(true);
    });
  });

  it("marca como falha quando a nova tentativa dá erro de rede", async () => {
    let calls = 0;
    server.use(
      sessionHandler(),
      http.post(URL_THUMBS, () => {
        calls += 1;
        return calls === 1
          ? HttpResponse.json({ urls: { "1": "https://thumbs.example.test/FAKE-1.jpg" } })
          : errorResponse("INTERNAL", "x", 500);
      }),
    );
    const { result } = renderHook(() => useThumbnails([1]), { wrapper: Wrapper });
    await waitFor(() => {
      expect(result.current.urls["1"]).toBeDefined();
    });

    act(() => {
      result.current.onImageError(1);
    });

    await waitFor(() => {
      expect(result.current.failed.has(1)).toBe(true);
    });
  });

  it("reinicia o controle de tentativas quando a página muda", async () => {
    const { api, handlers } = reviewApiHandlers();
    server.use(sessionHandler(), ...handlers);
    const { result, rerender } = renderHook(({ ids }) => useThumbnails(ids), {
      wrapper: Wrapper,
      initialProps: { ids: [1] },
    });
    await waitFor(() => {
      expect(result.current.urls["1"]).toBeDefined();
    });
    act(() => {
      result.current.onImageError(1);
    });
    await waitFor(() => {
      expect(result.current.urls["1"]).toContain("fresh");
    });

    rerender({ ids: [2] });
    await waitFor(() => {
      expect(result.current.urls["2"]).toBeDefined();
    });
    act(() => {
      result.current.onImageError(2);
    });

    await waitFor(() => {
      expect(result.current.urls["2"]).toContain("fresh");
    });
    expect(result.current.failed.size).toBe(0);
    expect(api.thumbnailBodies.filter((body) => body.refresh === true)).toHaveLength(2);
  });
});
