// SPDX-License-Identifier: GPL-3.0-only
import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { createQueryClient } from "../../api/queryClient";
import { errorResponse, sessionHandler, TEST_ORIGIN } from "../../test/handlers";
import { quarantineApiHandlers, type QuarantineApi } from "../../test/quarantine-fixtures";
import {
  CATEGORIES,
  reviewApiHandlers,
  reviewItem,
  type ReviewApi,
  type ReviewApiOptions,
} from "../../test/review-fixtures";
import { server } from "../../test/server";

import { ReviewPage } from "./ReviewPage";

const URL_LABELS = `${TEST_ORIGIN}/api/labels`;
const URL_REVIEW = `${TEST_ORIGIN}/api/review`;
const URL_CATEGORIES = `${TEST_ORIGIN}/api/categories`;
const PRINT = 2;

/** Em `server.use` o primeiro handler que casa prevalece: `overrides` vêm antes dos padrões. */
function setup(
  options: ReviewApiOptions = {},
  ...overrides: Parameters<typeof server.use>
): ReviewApi {
  const { api, handlers } = reviewApiHandlers(options);
  server.use(...overrides, sessionHandler(), ...handlers);
  render(
    <QueryClientProvider client={createQueryClient(0)}>
      <App>
        <ReviewPage />
      </App>
    </QueryClientProvider>,
  );
  return api;
}

function grid(): HTMLElement {
  return screen.getByRole("list", { name: "Itens para revisão" });
}

function card(id: number): HTMLElement {
  return screen.getByLabelText(new RegExp(`^Item ${String(id)}:`));
}

async function loaded(): Promise<void> {
  await screen.findByLabelText(/^Item 1:/);
  await screen.findByAltText("Miniatura do item 1");
}

function checkbox(id: number): HTMLElement {
  return screen.getByRole("checkbox", { name: `Selecionar item ${String(id)}` });
}

async function chooseOption(combobox: string, option: string): Promise<void> {
  await userEvent.click(screen.getByRole("combobox", { name: combobox }));
  await userEvent.click(await screen.findByTitle(option));
}

async function selectItems(...ids: number[]): Promise<void> {
  for (const id of ids) {
    await userEvent.click(checkbox(id));
  }
}

describe("ReviewPage: listagem", () => {
  it("mostra a fila ordenada por incerteza com sugestão e confiança", async () => {
    const api = setup();

    await loaded();

    expect(screen.getByRole("heading", { name: "Revisão" })).toBeInTheDocument();
    const queries = api.reviewQueries;
    expect(queries[0]?.get("order")).toBe("uncertainty");
    expect(queries[0]?.get("limit")).toBe("50");
    expect(within(grid()).getAllByRole("listitem")).toHaveLength(50);
    expect(within(card(1)).getByText("Foto real")).toBeInTheDocument();
    expect(within(card(2)).getByText("Print de tela")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Confiança 30%" })).toBeInTheDocument();
  });

  it("carrega as miniaturas da página em uma chamada", async () => {
    const api = setup();

    await loaded();

    await waitFor(() => {
      expect(screen.getByAltText("Miniatura do item 1")).toBeInTheDocument();
    });
    expect(api.thumbnailBodies).toHaveLength(1);
    expect(api.thumbnailBodies[0]?.item_ids).toHaveLength(50);
  });

  it("mostra o progresso até o retreino e a distância até a meta do Automático", async () => {
    setup();

    expect(await screen.findByText("15 / 20")).toBeInTheDocument();
    expect(screen.getByText("Meta ainda não atingida")).toBeInTheDocument();
    expect(screen.getByText("/ 0,98")).toBeInTheDocument();
  });

  it("filtra por categoria e reinicia a paginação", async () => {
    const api = setup();
    await loaded();

    await chooseOption("Filtrar por categoria", "Print de tela");

    await waitFor(() => {
      expect(api.reviewQueries.at(-1)?.get("category_id")).toBe(String(PRINT));
    });
    expect(api.reviewQueries.at(-1)?.get("cursor")).toBeNull();
  });

  it("limpa o filtro de categoria", async () => {
    const api = setup();
    await loaded();
    await chooseOption("Filtrar por categoria", "Print de tela");
    await waitFor(() => {
      expect(api.reviewQueries.at(-1)?.get("category_id")).toBe(String(PRINT));
    });

    fireEvent.mouseEnter(screen.getByRole("combobox", { name: "Filtrar por categoria" }));
    await userEvent.click(screen.getByRole("img", { name: "close-circle" }));

    await waitFor(() => {
      expect(api.reviewQueries.at(-1)?.get("category_id")).toBeNull();
    });
  });

  it("alterna a ordem para maior confiança", async () => {
    const api = setup();
    await loaded();

    await userEvent.click(screen.getByText("Maior confiança primeiro"));

    await waitFor(() => {
      expect(api.reviewQueries.at(-1)?.get("order")).toBe("confidence");
    });
  });

  it("pagina por cursor e volta à página anterior", async () => {
    const api = setup();
    await loaded();

    await userEvent.click(screen.getByRole("button", { name: "Page 2" }));

    await screen.findByLabelText(/^Item 51:/);
    expect(api.reviewQueries.at(-1)?.get("cursor")).toBe("50");
    await userEvent.click(screen.getByRole("button", { name: "Page 1" }));
    await screen.findByLabelText(/^Item 1:/);
    expect(api.reviewQueries.at(-1)?.get("cursor")).toBeNull();
  });

  it("muda o tamanho da página para 100 itens", async () => {
    const api = setup();
    await loaded();

    await userEvent.click(screen.getByRole("combobox", { name: "Page Size" }));
    await userEvent.click(await screen.findByTitle("100 / page"));

    await waitFor(() => {
      expect(api.reviewQueries.at(-1)?.get("limit")).toBe("100");
    });
    expect(await screen.findByLabelText(/^Item 100:/)).toBeInTheDocument();
  });

  it("mostra o estado vazio quando não há itens", async () => {
    setup({ total: 0 });

    expect(await screen.findByText("Nenhum item para revisar")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Selecionar página" })).toBeDisabled();
  });

  it("mostra erro com nova tentativa quando a fila falha", async () => {
    setup(
      {},
      http.get(URL_REVIEW, () => errorResponse("INTERNAL", "x", 400)),
    );

    expect(await screen.findByText("Falha ao carregar a fila de revisão")).toBeInTheDocument();
    expect(screen.getByText("Ocorreu um erro interno. Tente novamente.")).toBeInTheDocument();
    server.use(
      http.get(URL_REVIEW, () => HttpResponse.json({ items: [reviewItem(1)], next_cursor: null })),
    );
    await userEvent.click(screen.getByRole("button", { name: "Tentar novamente" }));
    expect(await screen.findByLabelText(/^Item 1:/)).toBeInTheDocument();
  });

  it("avisa quando a lista de categorias não está disponível", async () => {
    setup({ categories: null });

    expect(await screen.findByText(/Lista de categorias indisponível/)).toBeInTheDocument();
    await loaded();
    expect(within(card(1)).getByText("Categoria #1")).toBeInTheDocument();
  });

  it("avisa quando o estado do modelo não carrega", async () => {
    setup(
      {},
      http.get(`${TEST_ORIGIN}/api/models`, () => errorResponse("INTERNAL", "x", 400)),
    );

    expect(
      await screen.findByText("Não foi possível carregar o estado do modelo."),
    ).toBeInTheDocument();
  });
});

describe("ReviewPage: rótulos em lote", () => {
  it("seleciona 3 itens e classifica como Print de tela enviando 3 rótulos", async () => {
    const api = setup();
    await loaded();

    await selectItems(1, 2, 3);
    expect(screen.getByText("3 selecionados")).toBeInTheDocument();
    await chooseOption("Classificar como", "Print de tela");

    await waitFor(() => {
      expect(api.labelBodies).toEqual([
        {
          labels: [
            { item_id: 1, category_id: PRINT },
            { item_id: 2, category_id: PRINT },
            { item_id: 3, category_id: PRINT },
          ],
        },
      ]);
    });
    expect(
      await screen.findByText(/3 rótulos salvos\. Faltam 2 para o próximo retreino\./),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByLabelText(/^Item 1:/)).not.toBeInTheDocument();
    });
    expect(screen.queryByRole("toolbar")).not.toBeInTheDocument();
  });

  it("confirma a sugestão de cada item selecionado", async () => {
    const api = setup();
    await loaded();

    await selectItems(1, 2);
    await userEvent.click(screen.getByRole("button", { name: "Confirmar sugestão" }));

    await waitFor(() => {
      expect(api.labelBodies[0]?.labels).toEqual([
        { item_id: 1, category_id: 1 },
        { item_id: 2, category_id: 2 },
      ]);
    });
  });

  it("limpa a seleção pelo botão", async () => {
    setup();
    await loaded();
    await selectItems(1);

    await userEvent.click(screen.getByRole("button", { name: "Limpar seleção" }));

    expect(screen.queryByRole("toolbar")).not.toBeInTheDocument();
    expect(checkbox(1)).not.toBeChecked();
  });

  it("seleciona a página inteira pelo botão", async () => {
    setup();
    await loaded();

    await userEvent.click(screen.getByRole("button", { name: "Selecionar página" }));

    expect(screen.getByText("50 selecionados")).toBeInTheDocument();
  });

  it("singulariza o contador com um item selecionado", async () => {
    setup();
    await loaded();

    await selectItems(1);

    expect(screen.getByText("1 selecionado")).toBeInTheDocument();
  });

  it("notifica o retreino ao receber train_job_id", async () => {
    setup({ trainJobId: 9 });
    await loaded();

    await selectItems(1);
    await userEvent.click(screen.getByRole("button", { name: "Confirmar sugestão" }));

    expect(await screen.findByText("Retreino iniciado")).toBeInTheDocument();
    expect(screen.getByText(/O modelo está sendo reavaliado em segundo plano/)).toBeInTheDocument();
  });

  it("informa quantos rótulos faltam para o retreino", async () => {
    setup();
    await screen.findByText("15 / 20");
    await loaded();

    await selectItems(1);
    await userEvent.click(screen.getByRole("button", { name: "Confirmar sugestão" }));

    expect(
      await screen.findByText("1 rótulo salvo. Faltam 4 para o próximo retreino."),
    ).toBeInTheDocument();
  });

  it("não informa o que falta quando o estado do modelo não carregou", async () => {
    setup(
      {},
      http.get(`${TEST_ORIGIN}/api/models`, () => errorResponse("INTERNAL", "x", 400)),
    );
    await loaded();

    await selectItems(1);
    await userEvent.click(screen.getByRole("button", { name: "Confirmar sugestão" }));

    expect(await screen.findByText("1 rótulo salvo.")).toBeInTheDocument();
  });
});

describe("ReviewPage: falhas ao salvar", () => {
  it("mantém a seleção e permite tentar novamente", async () => {
    const api = setup();
    await loaded();
    server.use(http.post(URL_LABELS, () => errorResponse("INTERNAL", "x", 400)));
    await selectItems(1, 2);

    await userEvent.click(screen.getByRole("button", { name: "Confirmar sugestão" }));

    expect(await screen.findByText(/Não foi possível salvar os rótulos/)).toBeInTheDocument();
    expect(checkbox(1)).toBeChecked();
    expect(checkbox(2)).toBeChecked();

    server.use(
      http.post(URL_LABELS, async ({ request }) => {
        api.labelBodies.push((await request.json()) as ReviewApi["labelBodies"][number]);
        return HttpResponse.json({ saved: 2, train_job_id: null });
      }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Tentar novamente" }));

    await waitFor(() => {
      expect(api.labelBodies).toHaveLength(1);
    });
    await waitFor(() => {
      expect(screen.queryByText(/Não foi possível salvar os rótulos/)).not.toBeInTheDocument();
    });
  });

  it("explica categoria arquivada (422) e recarrega a lista de categorias", async () => {
    setup();
    await loaded();
    let categoryCalls = 0;
    server.use(
      http.post(URL_LABELS, () =>
        HttpResponse.json(
          {
            error: {
              code: "VALIDATION_ERROR",
              message: "Categoria arquivada ou inexistente.",
              details: { invalid_category_ids: [2] },
            },
          },
          { status: 422 },
        ),
      ),
      http.get(URL_CATEGORIES, () => {
        categoryCalls += 1;
        return HttpResponse.json({ categories: [] });
      }),
    );
    await selectItems(1);

    await userEvent.click(screen.getByRole("button", { name: "Confirmar sugestão" }));

    expect(await screen.findByText(/foi arquivada ou não existe mais/)).toBeInTheDocument();
    await waitFor(() => {
      expect(categoryCalls).toBeGreaterThan(0);
    });
  });

  it("trata erro de validação sem detalhes de categoria como erro genérico", async () => {
    setup();
    await loaded();
    server.use(
      http.post(URL_LABELS, () => errorResponse("VALIDATION_ERROR", "Itens inexistentes.", 422)),
    );
    await selectItems(1);

    await userEvent.click(screen.getByRole("button", { name: "Confirmar sugestão" }));

    expect(await screen.findByText("Os dados informados são inválidos.")).toBeInTheDocument();
  });
});

describe("ReviewPage: página esvaziada", () => {
  it("volta à página anterior quando a última página esvazia", async () => {
    const api = setup({ total: 60 });
    await loaded();
    await userEvent.click(screen.getByRole("button", { name: "Page 2" }));
    await screen.findByLabelText(/^Item 51:/);

    await userEvent.click(screen.getByRole("button", { name: "Selecionar página" }));
    await userEvent.click(screen.getByRole("button", { name: "Confirmar sugestão" }));

    await waitFor(() => {
      expect(api.reviewQueries.at(-1)?.get("cursor")).toBeNull();
    });
    expect(await screen.findByLabelText(/^Item 1:/)).toBeInTheDocument();
  });

  it("avança para a próxima página válida quando há mais itens adiante", async () => {
    let labeled = false;
    setup(
      {},
      http.get(URL_REVIEW, ({ request }) => {
        const cursor = new URL(request.url).searchParams.get("cursor");
        if (cursor === "next") {
          return HttpResponse.json({ items: [reviewItem(7)], next_cursor: null });
        }
        return labeled
          ? HttpResponse.json({ items: [], next_cursor: "next" })
          : HttpResponse.json({ items: [reviewItem(1)], next_cursor: null });
      }),
      http.post(URL_LABELS, () => {
        labeled = true;
        return HttpResponse.json({ saved: 1, train_job_id: null });
      }),
    );
    await loaded();

    await selectItems(1);
    await userEvent.click(screen.getByRole("button", { name: "Confirmar sugestão" }));

    expect(await screen.findByLabelText(/^Item 7:/)).toBeInTheDocument();
  });

  it("fica no estado vazio quando a primeira página esvazia", async () => {
    setup({ total: 1 });
    await loaded();

    await selectItems(1);
    await userEvent.click(screen.getByRole("button", { name: "Confirmar sugestão" }));

    expect(await screen.findByText("Nenhum item para revisar")).toBeInTheDocument();
  });
});

describe("ReviewPage: miniatura expirada", () => {
  it("tenta uma nova URL uma única vez e depois mostra o placeholder", async () => {
    const api = setup();
    const image = await screen.findByAltText("Miniatura do item 1");

    fireEvent.error(image);

    await waitFor(() => {
      expect(screen.getByAltText("Miniatura do item 1")).toHaveAttribute(
        "src",
        "https://thumbs.example.test/FAKE-1-fresh.jpg",
      );
    });
    expect(api.thumbnailBodies.filter((body) => body.refresh === true)).toEqual([
      { item_ids: [1], refresh: true },
    ]);

    fireEvent.error(screen.getByAltText("Miniatura do item 1"));

    await waitFor(() => {
      expect(screen.queryByAltText("Miniatura do item 1")).not.toBeInTheDocument();
    });
    expect(api.thumbnailBodies.filter((body) => body.refresh === true)).toHaveLength(1);
  });
});

describe("ReviewPage: atalhos de teclado", () => {
  it("Enter confirma a sugestão do card focado", async () => {
    const api = setup();
    await loaded();

    card(2).focus();
    await userEvent.keyboard("{Enter}");

    await waitFor(() => {
      expect(api.labelBodies).toEqual([{ labels: [{ item_id: 2, category_id: PRINT }] }]);
    });
  });

  it("Espaço seleciona o focado e as setas movem o foco", async () => {
    setup();
    await loaded();

    card(1).focus();
    await userEvent.keyboard("{ArrowRight} ");

    expect(card(2)).toHaveFocus();
    expect(checkbox(2)).toBeChecked();
    expect(card(2)).toHaveAttribute("tabindex", "0");
    expect(card(1)).toHaveAttribute("tabindex", "-1");
  });

  it("o número da categoria classifica o focado e a tecla A seleciona a página", async () => {
    const api = setup();
    await loaded();

    card(1).focus();
    await userEvent.keyboard("3");

    await waitFor(() => {
      expect(api.labelBodies).toEqual([{ labels: [{ item_id: 1, category_id: 3 }] }]);
    });
    card(2).focus();
    await userEvent.keyboard("a");
    expect(screen.getByText("50 selecionados")).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("toolbar")).not.toBeInTheDocument();
  });

  it("anuncia os atalhos para tecnologias assistivas", async () => {
    setup();
    await loaded();

    expect(card(1)).toHaveAttribute("aria-keyshortcuts", "Enter Space");
    expect(screen.getByRole("button", { name: "Selecionar página" })).toHaveAttribute(
      "aria-keyshortcuts",
      "a",
    );
  });
});

describe("ReviewPage: configurações", () => {
  it("usa o retrain_every configurado no progresso do retreino", async () => {
    setup(
      {},
      http.get(`${TEST_ORIGIN}/api/settings`, () =>
        HttpResponse.json({ retrain_every: 30, automation_mode: "evolutive" }),
      ),
    );

    expect(await screen.findByText("15 / 30")).toBeInTheDocument();
  });
});

describe("ReviewPage: mover para a quarentena", () => {
  function setupMove(
    options: ReviewApiOptions = {},
    ...overrides: Parameters<typeof server.use>
  ): { review: ReviewApi; quarantine: QuarantineApi } {
    const quarantine = quarantineApiHandlers();
    const review = setup(options, ...overrides, ...quarantine.handlers);
    return { review, quarantine: quarantine.api };
  }

  const MOVE = "Mover para quarentena";

  async function confirmMove(): Promise<void> {
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Mover para a quarentena" }));
  }

  it("confirma e envia os itens selecionados de categorias removíveis", async () => {
    const { quarantine } = setupMove();
    await loaded();
    await selectItems(2, 3);

    await userEvent.click(screen.getByRole("button", { name: MOVE }));
    expect(
      within(await screen.findByRole("dialog")).getAllByText("Mover 2 itens para a quarentena?"),
    ).not.toHaveLength(0);
    expect(quarantine.moveBodies).toHaveLength(0);
    await confirmMove();

    await waitFor(() => {
      expect(quarantine.moveBodies).toEqual([{ item_ids: [2, 3] }]);
    });
    expect(await screen.findByText(/2 itens enfileirados para a quarentena/)).toBeInTheDocument();
    expect(screen.queryByRole("toolbar")).not.toBeInTheDocument();
  });

  it("singulariza a confirmação e a mensagem com um item", async () => {
    const { quarantine } = setupMove();
    await loaded();
    await selectItems(2);

    await userEvent.click(screen.getByRole("button", { name: MOVE }));
    expect(
      within(await screen.findByRole("dialog")).getAllByText("Mover 1 item para a quarentena?"),
    ).not.toHaveLength(0);
    await confirmMove();

    expect(await screen.findByText(/1 item enfileirado para a quarentena/)).toBeInTheDocument();
    expect(quarantine.moveBodies).toHaveLength(1);
  });

  it("não envia nada quando o utilizador cancela a confirmação", async () => {
    const { quarantine } = setupMove();
    await loaded();
    await selectItems(2);
    await userEvent.click(screen.getByRole("button", { name: MOVE }));

    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancelar" }));

    expect(quarantine.moveBodies).toHaveLength(0);
    expect(screen.getByText("1 selecionado")).toBeInTheDocument();
  });

  it("desabilita a ação quando a seleção contém Foto real (RN-09)", async () => {
    setupMove();
    await loaded();

    await selectItems(2, 1);

    expect(screen.getByRole("button", { name: MOVE })).toBeDisabled();
    expect(screen.getByText(/Foto real \(categoria protegida\)/)).toBeInTheDocument();
  });

  it("habilita de novo ao tirar o item de Foto real da seleção", async () => {
    setupMove();
    await loaded();
    await selectItems(2, 1);

    await selectItems(1);

    expect(screen.getByRole("button", { name: MOVE })).toBeEnabled();
    expect(screen.queryByText(/Foto real \(categoria protegida\)/)).not.toBeInTheDocument();
  });

  it("considera o rótulo do utilizador acima da sugestão do modelo", async () => {
    setupMove(
      {},
      http.get(`${TEST_ORIGIN}/api/review`, () =>
        HttpResponse.json({
          items: [reviewItem(2, { user_category_id: 1 }), reviewItem(3)],
          next_cursor: null,
        }),
      ),
    );
    await screen.findByLabelText(/^Item 2:/);

    await selectItems(2);

    expect(screen.getByRole("button", { name: MOVE })).toBeDisabled();
  });

  it("desabilita a ação para categoria não removível que não é a protegida", async () => {
    const categories = CATEGORIES.map((category) =>
      category.id === PRINT ? { ...category, removable: false } : category,
    );
    setupMove({ categories });
    await loaded();

    await selectItems(2);

    expect(screen.getByRole("button", { name: MOVE })).toBeDisabled();
    expect(screen.getByText(/categoria não removível/)).toBeInTheDocument();
  });

  it("desabilita a ação quando a lista de categorias não carrega", async () => {
    setupMove({ categories: null });
    await loaded();

    await selectItems(2);

    expect(screen.getByRole("button", { name: MOVE })).toBeDisabled();
    expect(
      screen.getByText(/verificar se a seleção pode ir para a quarentena/),
    ).toBeInTheDocument();
  });

  it("lista os IDs bloqueados devolvidos em 422 e mantém a seleção", async () => {
    setupMove(
      {},
      http.post(`${TEST_ORIGIN}/api/quarantine/moves`, () =>
        HttpResponse.json(
          {
            error: {
              code: "PROTECTED_CATEGORY",
              message: "bloqueado",
              details: { blocked_item_ids: [3] },
            },
          },
          { status: 422 },
        ),
      ),
    );
    await loaded();
    await selectItems(2, 3);
    await userEvent.click(screen.getByRole("button", { name: MOVE }));

    await confirmMove();

    expect(
      await screen.findByText(/O servidor bloqueou 1 item da movimentação: 3\./),
    ).toBeInTheDocument();
    expect(screen.getByText("2 selecionados")).toBeInTheDocument();
  });

  it("mostra erro sem lista de bloqueados quando a movimentação falha por outro motivo", async () => {
    setupMove(
      {},
      http.post(`${TEST_ORIGIN}/api/quarantine/moves`, () => errorResponse("INTERNAL", "x", 500)),
    );
    await loaded();
    await selectItems(2);
    await userEvent.click(screen.getByRole("button", { name: MOVE }));

    await confirmMove();

    expect(await screen.findByText(/Ocorreu um erro interno/)).toBeInTheDocument();
    expect(screen.queryByText(/O servidor bloqueou/)).not.toBeInTheDocument();
    expect(screen.getByText("1 selecionado")).toBeInTheDocument();
  });

  it("ignora detalhes de erro que não trazem IDs numéricos", async () => {
    setupMove(
      {},
      http.post(`${TEST_ORIGIN}/api/quarantine/moves`, () =>
        HttpResponse.json(
          {
            error: {
              code: "VALIDATION_ERROR",
              message: "x",
              details: { blocked_item_ids: ["a"] },
            },
          },
          { status: 422 },
        ),
      ),
    );
    await loaded();
    await selectItems(2);
    await userEvent.click(screen.getByRole("button", { name: MOVE }));

    await confirmMove();

    expect(await screen.findByText(/Os dados informados são inválidos/)).toBeInTheDocument();
    expect(screen.queryByText(/O servidor bloqueou/)).not.toBeInTheDocument();
  });

  it("fecha o aviso de itens bloqueados", async () => {
    setupMove(
      {},
      http.post(`${TEST_ORIGIN}/api/quarantine/moves`, () =>
        HttpResponse.json(
          {
            error: { code: "VALIDATION_ERROR", message: "x", details: { blocked_item_ids: [2] } },
          },
          { status: 422 },
        ),
      ),
    );
    await loaded();
    await selectItems(2);
    await userEvent.click(screen.getByRole("button", { name: MOVE }));
    await confirmMove();
    await screen.findByText(/O servidor bloqueou/);

    await userEvent.click(screen.getByRole("button", { name: "close" }));

    await waitFor(() => {
      expect(screen.queryByText(/O servidor bloqueou/)).not.toBeInTheDocument();
    });
  });
});
