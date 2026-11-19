// SPDX-License-Identifier: GPL-3.0-only
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent, { PointerEventsCheckLevel } from "@testing-library/user-event";
import { App } from "antd";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import type { CategoryCreate, CategoryModel, CategoryPatch } from "../../api/categories";
import { createQueryClient } from "../../api/queryClient";
import { errorResponse, sessionHandler, TEST_ORIGIN } from "../../test/handlers";
import { CATEGORIES } from "../../test/review-fixtures";
import { server } from "../../test/server";

import { CategoriesPage } from "./CategoriesPage";

const URL_CATEGORIES = `${TEST_ORIGIN}/api/categories`;

interface CategoriesState {
  readonly posts: CategoryCreate[];
  readonly patches: { id: number; body: CategoryPatch }[];
}

/** Lista em memória: criar e arquivar alteram o que o GET devolve, como na API real. */
function categoriesHandlers(initial: readonly CategoryModel[] = CATEGORIES) {
  let list = [...initial];
  const state: CategoriesState = { posts: [], patches: [] };
  const handlers = [
    sessionHandler(),
    http.get(URL_CATEGORIES, () => HttpResponse.json({ categories: list })),
    http.post(URL_CATEGORIES, async ({ request }) => {
      const body = (await request.json()) as CategoryCreate;
      state.posts.push(body);
      const created: CategoryModel = {
        id: list.length + 1,
        slug: "novo",
        ...body,
        protected: false,
        archived: false,
        labels: 0,
      };
      list = [...list, created];
      return HttpResponse.json(created, { status: 201 });
    }),
    http.patch(`${URL_CATEGORIES}/:id`, async ({ request, params }) => {
      const id = Number(params.id);
      const body = (await request.json()) as CategoryPatch;
      state.patches.push({ id, body });
      list = list.map((item) =>
        item.id === id ? { ...item, ...(body as Partial<CategoryModel>) } : item,
      );
      return HttpResponse.json(list.find((item) => item.id === id));
    }),
  ];
  return { state, handlers };
}

function renderPage(): void {
  render(
    <QueryClientProvider client={createQueryClient(0)}>
      <App>
        <CategoriesPage />
      </App>
    </QueryClientProvider>,
  );
}

function row(name: string): HTMLElement {
  return screen.getByRole("row", { name: new RegExp(name) });
}

describe("CategoriesPage", () => {
  it("lista todas as categorias, incluindo Foto real com ações desabilitadas", async () => {
    server.use(...categoriesHandlers().handlers);
    renderPage();

    expect(await screen.findByRole("cell", { name: "Foto real" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "Documento antigo" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Editar Foto real" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Arquivar Foto real" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Editar Meme" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Arquivar Meme" })).toBeEnabled();
  });

  it("mostra o motivo (RN-09) ao passar o cursor sobre as ações de Foto real", async () => {
    server.use(...categoriesHandlers().handlers);
    renderPage();
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    await screen.findByRole("cell", { name: "Foto real" });

    await user.hover(screen.getByRole("button", { name: "Editar Foto real" }));

    expect(await screen.findByText(/Foto real é protegida \(RN-09\)/)).toBeInTheDocument();
  });

  it("desabilita arquivar em categoria já arquivada", async () => {
    server.use(...categoriesHandlers().handlers);
    renderPage();
    await screen.findByRole("cell", { name: "Documento antigo" });

    expect(screen.getByRole("button", { name: "Arquivar Documento antigo" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Editar Documento antigo" })).toBeEnabled();
  });

  it("mostra aviso quando a rota de categorias ainda não existe (404)", async () => {
    server.use(
      sessionHandler(),
      http.get(URL_CATEGORIES, () => errorResponse("NOT_FOUND", "Rota ausente", 404)),
    );
    renderPage();

    expect(await screen.findByText(/Lista de categorias indisponível/)).toBeInTheDocument();
  });

  it("cria categoria, fecha o Drawer e atualiza a tabela", async () => {
    const { handlers, state } = categoriesHandlers();
    server.use(...handlers);
    renderPage();
    const user = userEvent.setup();
    await screen.findByRole("cell", { name: "Foto real" });

    await user.click(screen.getByRole("button", { name: /Nova categoria/ }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Nome"), "Recibos");
    await user.type(within(dialog).getByLabelText("Descrição"), "Fotos de recibos");
    await user.click(within(dialog).getByRole("button", { name: "Salvar" }));

    expect(await screen.findByRole("cell", { name: "Recibos" })).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(state.posts).toEqual([
      { name: "Recibos", description: "Fotos de recibos", removable: true },
    ]);
  });

  it("arquiva após confirmação e mostra a categoria como arquivada", async () => {
    const { handlers, state } = categoriesHandlers();
    server.use(...handlers);
    renderPage();
    const user = userEvent.setup();
    await screen.findByRole("cell", { name: "Meme" });

    await user.click(screen.getByRole("button", { name: "Arquivar Meme" }));
    const confirm = await screen.findByRole("button", { name: "Arquivar" });
    await user.click(confirm);

    await waitFor(() => {
      expect(row("Meme")).toHaveTextContent("Arquivada");
    });
    expect(state.patches).toEqual([{ id: 3, body: { archived: true } }]);
  });

  it("não arquiva quando a confirmação é cancelada", async () => {
    const { handlers, state } = categoriesHandlers();
    server.use(...handlers);
    renderPage();
    const user = userEvent.setup();
    await screen.findByRole("cell", { name: "Meme" });

    await user.click(screen.getByRole("button", { name: "Arquivar Meme" }));
    await user.click(await screen.findByRole("button", { name: "Cancelar" }));

    expect(state.patches).toEqual([]);
  });

  it("abre a edição com os dados da categoria escolhida", async () => {
    server.use(...categoriesHandlers().handlers);
    renderPage();
    const user = userEvent.setup();
    await screen.findByRole("cell", { name: "Meme" });

    await user.click(screen.getByRole("button", { name: "Editar Meme" }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Editar categoria")).toBeInTheDocument();
    expect(within(dialog).getByLabelText("Nome")).toHaveValue("Meme");
  });

  it("mostra o erro quando o arquivamento falha e mantém a categoria ativa", async () => {
    server.use(
      sessionHandler(),
      http.get(URL_CATEGORIES, () => HttpResponse.json({ categories: CATEGORIES })),
      http.patch(`${URL_CATEGORIES}/:id`, () => errorResponse("INTERNAL", "x", 500)),
    );
    renderPage();
    const user = userEvent.setup();
    await screen.findByRole("cell", { name: "Meme" });

    await user.click(screen.getByRole("button", { name: "Arquivar Meme" }));
    await user.click(await screen.findByRole("button", { name: "Arquivar" }));

    expect(
      await screen.findByText("Ocorreu um erro interno. Tente novamente."),
    ).toBeInTheDocument();
    expect(row("Meme")).toHaveTextContent("Ativa");
  });
});
