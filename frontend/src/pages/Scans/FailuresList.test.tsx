// SPDX-License-Identifier: GPL-3.0-only
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { createQueryClient } from "../../api/queryClient";
import { errorResponse, sessionHandler, TEST_ORIGIN } from "../../test/handlers";
import { server } from "../../test/server";

import { FailuresList } from "./FailuresList";

function renderList(): void {
  render(
    <QueryClientProvider client={createQueryClient(0)}>
      <FailuresList />
    </QueryClientProvider>,
  );
}

function failuresPage(ids: number[], next: string | null) {
  return HttpResponse.json({
    items: ids.map((id) => ({
      item_id: id,
      failure_reason: id === 11 ? null : "decode_error",
      failure_count: id,
    })),
    next_cursor: next,
  });
}

describe("FailuresList", () => {
  it("informa quando não há falhas", async () => {
    server.use(
      sessionHandler(),
      http.get(`${TEST_ORIGIN}/api/failures`, () => failuresPage([], null)),
    );
    renderList();

    expect(await screen.findByText("Nenhuma falha de análise")).toBeInTheDocument();
  });

  it("envia os IDs selecionados ao tentar novamente", async () => {
    let body: unknown = null;
    server.use(
      sessionHandler(),
      http.get(`${TEST_ORIGIN}/api/failures`, () => failuresPage([1, 2, 11], null)),
      http.post(`${TEST_ORIGIN}/api/failures/retry`, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ job_ids: [4], requeued: 2, skipped: 0 }, { status: 202 });
      }),
    );
    const user = userEvent.setup();
    renderList();

    const retry = await screen.findByRole("button", { name: "Tentar novamente" });
    expect(retry).toBeDisabled();
    expect(screen.getByText(/Motivo não informado · 11 tentativas/)).toBeInTheDocument();
    expect(screen.getByText(/decode_error · 1 tentativa$/)).toBeInTheDocument();
    await user.click(screen.getByLabelText("Selecionar item 1"));
    await user.click(screen.getByLabelText("Selecionar item 2"));
    await user.click(screen.getByLabelText("Selecionar item 2"));
    await user.click(screen.getByLabelText("Selecionar item 11"));
    await user.click(retry);

    await waitFor(() => {
      expect(body).toEqual({ item_ids: [1, 11] });
    });
    expect(
      await screen.findByText("2 item(ns) reenfileirado(s); 0 ignorado(s)."),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Selecionar item 1")).not.toBeChecked();
  });

  it("mostra erro do reenvio", async () => {
    server.use(
      sessionHandler(),
      http.get(`${TEST_ORIGIN}/api/failures`, () => failuresPage([1], null)),
      http.post(`${TEST_ORIGIN}/api/failures/retry`, () => errorResponse("INTERNAL", "x", 500)),
    );
    const user = userEvent.setup();
    renderList();

    await user.click(await screen.findByLabelText("Selecionar item 1"));
    await user.click(screen.getByRole("button", { name: "Tentar novamente" }));

    expect(await screen.findByText(/erro interno/)).toBeInTheDocument();
  });

  it("carrega mais páginas", async () => {
    server.use(
      sessionHandler(),
      http.get(`${TEST_ORIGIN}/api/failures`, ({ request }) =>
        new URL(request.url).searchParams.get("cursor") === null
          ? failuresPage([1], "next")
          : failuresPage([2], null),
      ),
    );
    const user = userEvent.setup();
    renderList();

    await user.click(await screen.findByRole("button", { name: "Carregar mais" }));

    expect(await screen.findByLabelText("Selecionar item 2")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Carregar mais" })).not.toBeInTheDocument();
  });

  it("mostra erro de carregamento e recarrega", async () => {
    let fail = true;
    server.use(
      sessionHandler(),
      http.get(`${TEST_ORIGIN}/api/failures`, () =>
        fail ? errorResponse("INTERNAL", "x", 500) : failuresPage([1], null),
      ),
    );
    const user = userEvent.setup();
    renderList();

    const retry = await screen.findByRole("button", { name: "Tentar novamente" });
    fail = false;
    await user.click(retry);

    expect(await screen.findByLabelText("Selecionar item 1")).toBeInTheDocument();
  });
});
