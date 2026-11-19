// SPDX-License-Identifier: GPL-3.0-only
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import { delay, http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import { createQueryClient } from "../../api/queryClient";
import { sessionHandler, TEST_ORIGIN } from "../../test/handlers";
import {
  quarantineApiHandlers,
  quarantineItem,
  type QuarantineApi,
  type QuarantineApiOptions,
} from "../../test/quarantine-fixtures";
import { server } from "../../test/server";

import { DeleteConfirmModal } from "./DeleteConfirmModal";

const IDS = [1, 2, 3] as const;
const SEND = "Enviar para a lixeira";

interface Setup {
  readonly api: QuarantineApi;
  readonly onClose: ReturnType<typeof vi.fn>;
  readonly onDeleted: ReturnType<typeof vi.fn>;
}

function setup(
  options: QuarantineApiOptions = {},
  ...overrides: Parameters<typeof server.use>
): Setup {
  const { api, handlers } = quarantineApiHandlers({
    items: IDS.map((id) => quarantineItem(id)),
    ...options,
  });
  server.use(...overrides, sessionHandler(), ...handlers);
  const onClose = vi.fn();
  const onDeleted = vi.fn();
  render(
    <QueryClientProvider client={createQueryClient(0)}>
      <App>
        <DeleteConfirmModal open itemIds={IDS} onClose={onClose} onDeleted={onDeleted} />
      </App>
    </QueryClientProvider>,
  );
  return { api, onClose, onDeleted };
}

function sendButton(): HTMLElement {
  return screen.getByRole("button", { name: SEND });
}

function field(): HTMLElement {
  return screen.getByLabelText("Digite a quantidade de itens para confirmar");
}

describe("DeleteConfirmModal", () => {
  it("não renderiza nada nem chama a API enquanto fechado", () => {
    const { api, handlers } = quarantineApiHandlers();
    server.use(sessionHandler(), ...handlers);

    render(
      <QueryClientProvider client={createQueryClient(0)}>
        <App>
          <DeleteConfirmModal open={false} itemIds={IDS} onClose={vi.fn()} onDeleted={vi.fn()} />
        </App>
      </QueryClientProvider>,
    );

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(api.previewBodies).toHaveLength(0);
  });

  it("pede o preview e mostra quantidade e volume a excluir", async () => {
    const { api } = setup();

    expect(await screen.findByText("Itens a excluir")).toBeInTheDocument();

    expect(api.previewBodies).toEqual([{ item_ids: [1, 2, 3] }]);
    expect(screen.getByText("Volume a excluir")).toBeInTheDocument();
    expect(screen.getByText("3 MB")).toBeInTheDocument();
    expect(sendButton()).toBeDisabled();
  });

  it("mantém o botão desabilitado com a quantidade errada", async () => {
    setup();
    await screen.findByText("Itens a excluir");

    await userEvent.type(field(), "2");

    expect(sendButton()).toBeDisabled();
  });

  it("habilita só com a quantidade exata e envia o token do preview", async () => {
    const { api, onDeleted } = setup();
    await screen.findByText("Itens a excluir");

    await userEvent.type(field(), "3");
    expect(sendButton()).toBeEnabled();
    await userEvent.click(sendButton());

    await waitFor(() => {
      expect(onDeleted).toHaveBeenCalledWith(3);
    });
    expect(api.deletionBodies).toEqual([
      { item_ids: [1, 2, 3], confirmation_token: "FAKE-token-1" },
    ]);
  });

  it("confirma pelo Enter quando a quantidade confere e ignora o Enter antes disso", async () => {
    const { api, onDeleted } = setup();
    await screen.findByText("Itens a excluir");

    await userEvent.type(field(), "2{Enter}");
    expect(api.deletionBodies).toHaveLength(0);
    await userEvent.clear(field());
    await userEvent.type(field(), "3{Enter}");

    await waitFor(() => {
      expect(onDeleted).toHaveBeenCalledTimes(1);
    });
  });

  it("bloqueia o duplo clique durante a requisição", async () => {
    const deletions: unknown[] = [];
    const { onDeleted } = setup(
      {},
      http.post(`${TEST_ORIGIN}/api/quarantine/deletions`, async ({ request }) => {
        deletions.push(await request.json());
        await delay(50);
        return HttpResponse.json({ job_id: 5 }, { status: 202 });
      }),
    );
    await screen.findByText("Itens a excluir");
    await userEvent.type(field(), "3");

    await userEvent.dblClick(sendButton());

    await waitFor(() => {
      expect(onDeleted).toHaveBeenCalledTimes(1);
    });
    expect(deletions).toHaveLength(1);
  });

  it("refaz o preview e pede nova confirmação quando o token expirou", async () => {
    const { api, onDeleted } = setup({
      tokens: ["FAKE-old", "FAKE-new"],
      expiredTokens: ["FAKE-old"],
    });
    await screen.findByText("Itens a excluir");
    await userEvent.type(field(), "3");

    await userEvent.click(sendButton());

    expect(await screen.findByText(/A confirmação expirou/)).toBeInTheDocument();
    await waitFor(() => {
      expect(api.previewBodies).toHaveLength(2);
    });
    await waitFor(() => {
      expect(field()).toHaveValue("");
    });
    expect(sendButton()).toBeDisabled();
    expect(onDeleted).not.toHaveBeenCalled();

    await userEvent.type(field(), "3");
    await userEvent.click(sendButton());

    await waitFor(() => {
      expect(onDeleted).toHaveBeenCalledTimes(1);
    });
    expect(api.deletionBodies.at(-1)?.confirmation_token).toBe("FAKE-new");
  });

  it("mostra o erro e mantém o modal aberto quando a exclusão falha", async () => {
    const { onDeleted } = setup({ deletionStatus: 500 });
    await screen.findByText("Itens a excluir");
    await userEvent.type(field(), "3");

    await userEvent.click(sendButton());

    expect(await screen.findByText(/Ocorreu um erro interno/)).toBeInTheDocument();
    expect(onDeleted).not.toHaveBeenCalled();
    expect(sendButton()).toBeEnabled();
  });

  it("mostra erro do preview com nova tentativa", async () => {
    const { api } = setup({ previewStatus: 422 });

    expect(await screen.findByText(/O item não está na quarentena/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Digite a quantidade de itens para confirmar")).toBeNull();
    expect(sendButton()).toBeDisabled();

    await userEvent.click(screen.getByRole("button", { name: "Tentar novamente" }));

    await waitFor(() => {
      expect(api.previewBodies).toHaveLength(2);
    });
  });

  it("fecha pelo Cancelar", async () => {
    const { onClose } = setup();
    await screen.findByText("Itens a excluir");

    await userEvent.click(screen.getByRole("button", { name: "Cancelar" }));

    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
