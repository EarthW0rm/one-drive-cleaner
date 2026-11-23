// SPDX-License-Identifier: GPL-3.0-only
import { QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { errorResponse, sessionHandler, TEST_ORIGIN } from "../test/handlers";
import { server } from "../test/server";

import {
  hasActiveJob,
  MAX_POLL_ERRORS,
  POLL_INTERVAL_MS,
  useFailures,
  useJobs,
  usePauseJob,
  useResumeJob,
  useRetryFailures,
  useStartScan,
  type JobModel,
} from "./jobs";
import { createQueryClient } from "./queryClient";

function job(status: string, id = 1): JobModel {
  return {
    id,
    type: "scan",
    status,
    progress: { done: 0, total: 1 },
    pause_reason: null,
    error_code: null,
  };
}

function wrapper({ children }: Readonly<{ children: ReactNode }>): ReactNode {
  return <QueryClientProvider client={createQueryClient(0)}>{children}</QueryClientProvider>;
}

afterEach(() => {
  vi.useRealTimers();
});

describe("hasActiveJob", () => {
  it.each([
    [undefined, false],
    [[], false],
    [[job("done"), job("paused", 2)], false],
    [[job("done"), job("queued", 2)], true],
    [[job("running")], true],
  ])("%j -> %s", (jobs, expected) => {
    expect(hasActiveJob(jobs)).toBe(expected);
  });
});

describe("useJobs", () => {
  function countingJobs(responses: (call: number) => Response): { calls: () => number } {
    let calls = 0;
    server.use(
      sessionHandler(),
      http.get(`${TEST_ORIGIN}/api/jobs`, () => {
        calls += 1;
        return responses(calls);
      }),
    );
    return { calls: () => calls };
  }

  it("não faz polling quando não há job ativo", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const counter = countingJobs(() => HttpResponse.json({ jobs: [job("done")] }));
    const { result } = renderHook(() => useJobs(), { wrapper });
    await waitFor(() => {
      expect(result.current.data).toHaveLength(1);
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 3);
    });

    expect(counter.calls()).toBe(1);
  });

  it("repete a cada 2 s enquanto há job running e para ao terminar", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const counter = countingJobs((call) =>
      HttpResponse.json({ jobs: [job(call < 3 ? "running" : "done")] }),
    );
    const { result } = renderHook(() => useJobs(), { wrapper });
    await waitFor(() => {
      expect(counter.calls()).toBe(1);
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 6);
    });

    expect(counter.calls()).toBe(3);
    expect(result.current.data?.[0]?.status).toBe("done");
  });

  it("para após 3 erros seguidos e retoma com restartPolling", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let failing = false;
    const counter = countingJobs(() =>
      failing
        ? errorResponse("INTERNAL", "falha", 500)
        : HttpResponse.json({ jobs: [job("running")] }),
    );
    const { result } = renderHook(() => useJobs(), { wrapper });
    await waitFor(() => {
      expect(counter.calls()).toBe(1);
    });
    failing = true;

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * (MAX_POLL_ERRORS + 4));
    });

    expect(counter.calls()).toBe(1 + MAX_POLL_ERRORS);
    expect(result.current.pollingStopped).toBe(true);

    failing = false;
    act(() => {
      result.current.restartPolling();
    });
    await waitFor(() => {
      expect(result.current.pollingStopped).toBe(false);
    });
    expect(result.current.isError).toBe(false);
  });
});

describe("mutações", () => {
  it("useStartScan envia o modo e devolve o job_id", async () => {
    const bodies: unknown[] = [];
    server.use(
      sessionHandler(),
      http.post(`${TEST_ORIGIN}/api/scans`, async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json({ job_id: 7 }, { status: 202 });
      }),
    );
    const { result } = renderHook(() => useStartScan(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync("full");
    });

    expect(bodies).toEqual([{ mode: "full" }]);
  });

  it("pause e resume chamam as rotas do job", async () => {
    const paths: string[] = [];
    server.use(
      sessionHandler(),
      http.post(`${TEST_ORIGIN}/api/jobs/:id/:action`, ({ params }) => {
        paths.push(`${String(params.id)}/${String(params.action)}`);
        return HttpResponse.json({});
      }),
    );
    const { result: pauseResult } = renderHook(() => usePauseJob(), { wrapper });
    const { result: resumeResult } = renderHook(() => useResumeJob(), { wrapper });

    await act(async () => {
      await pauseResult.current.mutateAsync(3);
      await resumeResult.current.mutateAsync(4);
    });

    expect(paths).toEqual(["3/pause", "4/resume"]);
  });
});

describe("falhas", () => {
  it("useFailures segue o cursor e useRetryFailures envia os ids", async () => {
    const cursors: (string | null)[] = [];
    let retried: unknown = null;
    server.use(
      sessionHandler(),
      http.get(`${TEST_ORIGIN}/api/failures`, ({ request }) => {
        const cursor = new URL(request.url).searchParams.get("cursor");
        cursors.push(cursor);
        return HttpResponse.json({
          items: [{ item_id: cursor === null ? 1 : 2, failure_reason: null, failure_count: 1 }],
          next_cursor: cursor === null ? "c 1" : null,
        });
      }),
      http.post(`${TEST_ORIGIN}/api/failures/retry`, async ({ request }) => {
        retried = await request.json();
        return HttpResponse.json({ job_ids: [1], requeued: 2, skipped: 0 }, { status: 202 });
      }),
    );
    const { result: failuresResult } = renderHook(() => useFailures(), { wrapper });
    await waitFor(() => {
      expect(failuresResult.current.hasNextPage).toBe(true);
    });
    await act(async () => {
      await failuresResult.current.fetchNextPage();
    });
    const { result: retryResult } = renderHook(() => useRetryFailures(), { wrapper });
    await act(async () => {
      await retryResult.current.mutateAsync([1, 2]);
    });

    expect(cursors).toEqual([null, "c 1"]);
    expect(retried).toEqual({ item_ids: [1, 2] });
  });
});
