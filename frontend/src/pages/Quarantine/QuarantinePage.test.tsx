// SPDX-License-Identifier: GPL-3.0-only
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { createQueryClient } from "../../api/queryClient";
import { sessionHandler, TEST_ORIGIN } from "../../test/handlers";
import {
  quarantineApiHandlers,
  quarantineItem,
  restoreNotice,
  type QuarantineApi,
  type QuarantineApiOptions,
} from "../../test/quarantine-fixtures";
import { reviewApiHandlers } from "../../test/review-fixtures";
import { server } from "../../test/server";

import { QuarantinePage } from "./QuarantinePage";

const SELECT_ALL = 0;

function items(count: number, overrides: Parameters<typeof quarantineItem>[1] = {}) {
  return Array.from({ length: count }, (_, index) => quarantineItem(index + 1, overrides));
}

function setup(
  options: QuarantineApiOptions = {},
  ...overrides: Parameters<typeof server.use>
): QuarantineApi {
  const { api, handlers } = quarantineApiHandlers(options);
  server.use(...overrides, sessionHandler(), ...handlers, ...reviewApiHandlers().handlers);
  render(
    <QueryClientProvider client={createQueryClient(0)}>
      <App>
        <QuarantinePage />
      </App>
    </QueryClientProvider>,
  );
  return api;
}

async function loaded(): Promise<void> {
  await screen.findByAltText("Miniatura do item 1");
}

/**
 * Espera a página carregar de fato: a miniatura do primeiro item só aparece depois do fetch da
 * lista. O rótulo "Página N" muda antes (estado local) e, durante o carregamento, a tabela fica
 * com `pointer-events: none` e "Próxima" desabilitada — esperar só pelo rótulo gera corrida.
 */
async function pageLoaded(firstItemId: number): Promise<void> {
  await screen.findByAltText(`Miniatura do item ${String(firstItemId)}`);
}

function selectAllBox(): HTMLElement {
  const [box] = screen.getAllByRole("checkbox");
  if (box === undefined) {
    throw new Error("Checkbox de seleção da tabela não encontrado");
  }
  return box;
}

async function selectRows(...positions: number[]): Promise<void> {
  const boxes = screen.getAllByRole("checkbox");
  for (const position of positions) {
    const box = boxes[position];
    if (box !== undefined) {
      await userEvent.click(box);
    }
  }
}

async function chooseCategory(combobox: string, option: string): Promise<void> {
  await userEvent.click(screen.getByRole("combobox", { name: combobox }));
  await userEvent.click(await screen.findByTitle(option));
}

describe("QuarantinePage: listagem", () => {
  it("mostra itens, volume, grupos por categoria e as linhas com miniatura", async () => {
    setup({ items: items(3) });

    await loaded();

    expect(screen.getByRole("heading", { name: "Quarentena" })).toBeInTheDocument();
    expect(screen.getByText("Itens")).toBeInTheDocument();
    expect(screen.getByText("3 MB")).toBeInTheDocument();
    const groups = screen.getByLabelText("Itens por categoria");
    expect(within(groups).getByText(/Meme: 2 · 2 MB/)).toBeInTheDocument();
    expect(within(groups).getByText(/Print de tela: 1 · 1 MB/)).toBeInTheDocument();
    expect(screen.getAllByText("Manual")).toHaveLength(3);
    expect(screen.getAllByText("1 MB")).toHaveLength(3);
  });

  it("mostra o modo automático, categoria desconhecida e item sem data nem categoria", async () => {
    setup({
      items: [
        quarantineItem(1, { mode: "auto", category_id: 99 }),
        quarantineItem(2, { mode: "outro", category_id: null, moved_at: null }),
      ],
    });

    await loaded();

    expect(screen.getByText("Automático")).toBeInTheDocument();
    expect(screen.getByText("outro")).toBeInTheDocument();
    expect(screen.getAllByText("Categoria #99").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Sem categoria").length).toBeGreaterThan(0);
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("mostra o estado vazio", async () => {
    setup({ items: [] });

    expect(await screen.findByText("A quarentena está vazia")).toBeInTheDocument();
  });

  it("mostra erro com nova tentativa quando a listagem falha", async () => {
    setup({ listStatus: 500 });

    expect(await screen.findByText("Falha ao carregar a quarentena")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Tentar novamente" })).toBeInTheDocument();
  });

  it("usa o placeholder quando a miniatura não existe", async () => {
    setup(
      { items: items(1) },
      http.post(`${TEST_ORIGIN}/api/thumbnails`, () => HttpResponse.json({ urls: {} })),
    );

    expect(await screen.findAllByLabelText("Miniatura indisponível")).not.toHaveLength(0);
  });

  it("filtra por categoria e volta ao início da lista", async () => {
    const api = setup({ items: items(4) });
    await loaded();

    await chooseCategory("Filtrar por categoria", "Print de tela (2)");

    await waitFor(() => {
      expect(api.listQueries.at(-1)?.get("category_id")).toBe("2");
    });
    await waitFor(() => {
      expect(screen.getAllByText("2 MB").length).toBeGreaterThan(0);
    });
  });

  it("limpa o filtro de categoria", async () => {
    const api = setup({ items: items(4) });
    await loaded();
    await chooseCategory("Filtrar por categoria", "Meme (2)");
    await waitFor(() => {
      expect(api.listQueries.at(-1)?.get("category_id")).toBe("3");
    });

    await userEvent.hover(screen.getByRole("combobox", { name: "Filtrar por categoria" }));
    await userEvent.click(await screen.findByRole("img", { name: "close-circle" }));

    await waitFor(() => {
      expect(api.listQueries.at(-1)?.has("category_id")).toBe(false);
    });
  });

  it("pagina por cursor e volta à página anterior", async () => {
    const api = setup({ items: items(60) });
    await pageLoaded(60);
    expect(screen.getByText("Página 1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Anterior" })).toBeDisabled();

    await userEvent.click(screen.getByRole("button", { name: "Próxima" }));

    await pageLoaded(10);
    expect(screen.getByText("Página 2")).toBeInTheDocument();
    expect(api.listQueries.at(-1)?.get("cursor")).toBe("11");
    expect(screen.getByRole("button", { name: "Próxima" })).toBeDisabled();

    await userEvent.click(screen.getByRole("button", { name: "Anterior" }));

    await pageLoaded(60);
    expect(screen.getByText("Página 1")).toBeInTheDocument();
  });

  it("atualiza a lista pelo botão", async () => {
    const api = setup({ items: items(2) });
    await loaded();
    const before = api.listQueries.length;

    await userEvent.click(screen.getByRole("button", { name: /Atualizar/ }));

    await waitFor(() => {
      expect(api.listQueries.length).toBeGreaterThan(before);
    });
  });
});

describe("QuarantinePage: avisos de restauração", () => {
  it("lista restaurados automaticamente (RN-17) e por fallback (RN-19)", async () => {
    setup({
      items: items(1),
      notices: [
        restoreNotice(7),
        restoreNotice(8, { restore_reason: "false_positive", restore_fallback: true }),
      ],
    });

    expect(await screen.findByText(/1 item foi restaurado automaticamente/)).toBeInTheDocument();
    expect(screen.getByText(/1 item foi restaurado na pasta Restaurados/)).toBeInTheDocument();
    expect(screen.getByText(/Item 7 \(Foto real\)/)).toBeInTheDocument();
    expect(screen.getByText(/Item 8 \(Foto real\)/)).toBeInTheDocument();
  });

  it("não mostra avisos quando não há restaurações", async () => {
    setup({ items: items(1) });
    await loaded();

    expect(screen.queryByText(/restaurado/)).not.toBeInTheDocument();
  });
});

describe("QuarantinePage: restaurar", () => {
  it("mantém as ações desabilitadas sem seleção", async () => {
    setup({ items: items(2) });
    await loaded();

    expect(screen.getByRole("button", { name: "Restaurar" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Excluir" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Limpar seleção" })).toBeDisabled();
  });

  it("exige a categoria e envia restores com a categoria escolhida", async () => {
    const api = setup({ items: items(3) });
    await loaded();
    await selectRows(1, 2);
    expect(screen.getByText("2 selecionados")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Restaurar" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Restaurar" }));

    expect(await within(dialog).findByText(/Escolha a categoria correta/)).toBeInTheDocument();
    expect(api.restoreBodies).toHaveLength(0);

    await chooseCategory("Categoria correta", "Foto real");
    await userEvent.click(within(dialog).getByRole("button", { name: "Restaurar" }));

    await waitFor(() => {
      expect(api.restoreBodies).toEqual([
        {
          restores: [
            { item_id: 3, category_id: 1 },
            { item_id: 2, category_id: 1 },
          ],
        },
      ]);
    });
    expect(await screen.findByText(/2 itens enfileirados para restauração/)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(screen.getByText("0 selecionados")).toBeInTheDocument();
  });

  it("singulariza a mensagem com um item e permite cancelar", async () => {
    setup({ items: items(2) });
    await loaded();
    await selectRows(1);
    await userEvent.click(screen.getByRole("button", { name: "Restaurar" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/1 item volta à pasta/)).toBeInTheDocument();

    await userEvent.click(within(dialog).getByRole("button", { name: "Cancelar" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
  });

  it("não oferece categoria arquivada na restauração", async () => {
    setup({ items: items(2) });
    await loaded();
    await selectRows(1);
    await userEvent.click(screen.getByRole("button", { name: "Restaurar" }));
    await userEvent.click(await screen.findByRole("combobox", { name: "Categoria correta" }));

    expect(await screen.findByTitle("Meme")).toBeInTheDocument();
    expect(screen.queryByTitle("Documento antigo")).not.toBeInTheDocument();
  });

  it("explica a categoria arquivada (422) e mantém o modal aberto", async () => {
    setup(
      { items: items(2) },
      http.post(`${TEST_ORIGIN}/api/quarantine/restores`, () =>
        HttpResponse.json(
          { error: { code: "VALIDATION_ERROR", message: "x", details: null } },
          { status: 422 },
        ),
      ),
    );
    await loaded();
    await selectRows(1);
    await userEvent.click(screen.getByRole("button", { name: "Restaurar" }));
    const dialog = await screen.findByRole("dialog");
    await chooseCategory("Categoria correta", "Meme");

    await userEvent.click(within(dialog).getByRole("button", { name: "Restaurar" }));

    expect(await within(dialog).findByText(/foi arquivada ou não existe mais/)).toBeInTheDocument();
  });

  it("mostra erro genérico quando a restauração falha", async () => {
    setup({ items: items(2), restoreStatus: 500 });
    await loaded();
    await selectRows(1);
    await userEvent.click(screen.getByRole("button", { name: "Restaurar" }));
    const dialog = await screen.findByRole("dialog");
    await chooseCategory("Categoria correta", "Meme");

    await userEvent.click(within(dialog).getByRole("button", { name: "Restaurar" }));

    expect(await within(dialog).findByText(/Ocorreu um erro interno/)).toBeInTheDocument();
  });

  it("limita a restauração a 500 itens por vez", async () => {
    const total = 501;
    const pageSize = 50;
    setup({ items: items(total) });
    await pageLoaded(total);

    // A lista é decrescente por id: a página `p` começa em `total - p * pageSize`.
    for (let page = 0; page * pageSize < total; page += 1) {
      await pageLoaded(total - page * pageSize);
      await userEvent.click(selectAllBox());
      if (screen.getByRole("button", { name: "Próxima" }).hasAttribute("disabled")) {
        break;
      }
      await userEvent.click(screen.getByRole("button", { name: "Próxima" }));
    }

    expect(screen.getByText("501 selecionados")).toBeInTheDocument();
    expect(screen.getByText("Restaure no máximo 500 itens por vez.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restaurar" })).toBeDisabled();
  }, 60_000);

  it("limpa a seleção pelo botão", async () => {
    setup({ items: items(2) });
    await loaded();
    await selectRows(SELECT_ALL);
    expect(screen.getByText("2 selecionados")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Limpar seleção" }));

    expect(screen.getByText("0 selecionados")).toBeInTheDocument();
  });
});

describe("QuarantinePage: excluir", () => {
  it("abre a confirmação com quantidade e volume e exclui com a quantidade digitada", async () => {
    const api = setup({ items: items(3) });
    await loaded();
    await selectRows(SELECT_ALL);

    await userEvent.click(screen.getByRole("button", { name: "Excluir" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("Itens a excluir")).toBeInTheDocument();
    expect(api.previewBodies).toEqual([{ item_ids: [3, 2, 1] }]);
    await userEvent.type(
      within(dialog).getByLabelText("Digite a quantidade de itens para confirmar"),
      "3",
    );
    await userEvent.click(within(dialog).getByRole("button", { name: "Enviar para a lixeira" }));

    await waitFor(() => {
      expect(api.deletionBodies).toHaveLength(1);
    });
    expect(await screen.findByText(/3 itens seguem para a lixeira/)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(screen.getByText("0 selecionados")).toBeInTheDocument();
  });

  it("singulariza a mensagem com um item e permite cancelar", async () => {
    setup({ items: items(2) });
    await loaded();
    await selectRows(1);
    await userEvent.click(screen.getByRole("button", { name: "Excluir" }));
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByText("Itens a excluir");

    await userEvent.click(within(dialog).getByRole("button", { name: "Cancelar" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
  });
});
