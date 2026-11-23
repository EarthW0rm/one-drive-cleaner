// SPDX-License-Identifier: GPL-3.0-only
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import { CATEGORIES_KEY } from "../../api/categories";
import { createQueryClient } from "../../api/queryClient";
import { sessionHandler, TEST_ORIGIN } from "../../test/handlers";
import { quarantineApiHandlers, type QuarantineApiOptions } from "../../test/quarantine-fixtures";
import { CATEGORIES } from "../../test/review-fixtures";
import { server } from "../../test/server";

import { RestoreModal } from "./RestoreModal";

const IDS = [4, 5] as const;
const ACTIVE = CATEGORIES.filter((category) => !category.archived);

function setup(
  open = true,
  options: QuarantineApiOptions = {},
  ...overrides: Parameters<typeof server.use>
) {
  const { api, handlers } = quarantineApiHandlers(options);
  server.use(...overrides, sessionHandler(), ...handlers);
  const client = createQueryClient(0);
  client.setQueryData(CATEGORIES_KEY, CATEGORIES);
  const onClose = vi.fn();
  const onRestored = vi.fn();
  render(
    <QueryClientProvider client={client}>
      <App>
        <RestoreModal
          open={open}
          itemIds={IDS}
          categories={ACTIVE}
          onClose={onClose}
          onRestored={onRestored}
        />
      </App>
    </QueryClientProvider>,
  );
  return { api, client, onClose, onRestored };
}

async function pick(option: string): Promise<void> {
  await userEvent.click(screen.getByRole("combobox", { name: "Categoria correta" }));
  await userEvent.click(await screen.findByTitle(option));
}

describe("RestoreModal", () => {
  it("não renderiza fechado", () => {
    setup(false);

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("exige a categoria antes de restaurar", async () => {
    const { api } = setup();

    await userEvent.click(screen.getByRole("button", { name: "Restaurar" }));

    expect(
      await screen.findByText("Escolha a categoria correta para restaurar."),
    ).toBeInTheDocument();
    expect(api.restoreBodies).toHaveLength(0);
  });

  it("envia um restore por item com a categoria escolhida", async () => {
    const { api, onRestored } = setup();

    await pick("Print de tela");
    await userEvent.click(screen.getByRole("button", { name: "Restaurar" }));

    await waitFor(() => {
      expect(onRestored).toHaveBeenCalledWith(2);
    });
    expect(api.restoreBodies).toEqual([
      {
        restores: [
          { item_id: 4, category_id: 2 },
          { item_id: 5, category_id: 2 },
        ],
      },
    ]);
  });

  it("explica a categoria arquivada (422) e recarrega a lista de categorias", async () => {
    const { client, onRestored } = setup(
      true,
      {},
      http.post(`${TEST_ORIGIN}/api/quarantine/restores`, () =>
        HttpResponse.json(
          { error: { code: "VALIDATION_ERROR", message: "x", details: null } },
          { status: 422 },
        ),
      ),
    );

    await pick("Meme");
    await userEvent.click(screen.getByRole("button", { name: "Restaurar" }));

    expect(await screen.findByText(/foi arquivada ou não existe mais/)).toBeInTheDocument();
    expect(client.getQueryState(CATEGORIES_KEY)?.isInvalidated).toBe(true);
    expect(onRestored).not.toHaveBeenCalled();
  });

  it("mostra a mensagem do erro quando a restauração falha por outro motivo", async () => {
    setup(true, { restoreStatus: 500 });

    await pick("Meme");
    await userEvent.click(screen.getByRole("button", { name: "Restaurar" }));

    expect(await screen.findByText(/Ocorreu um erro interno/)).toBeInTheDocument();
  });

  it("fecha pelo Cancelar", async () => {
    const { onClose } = setup();

    await userEvent.click(screen.getByRole("button", { name: "Cancelar" }));

    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
