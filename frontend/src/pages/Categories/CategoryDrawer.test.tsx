// SPDX-License-Identifier: GPL-3.0-only
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import type { CategoryModel } from "../../api/categories";
import { createQueryClient } from "../../api/queryClient";
import { errorResponse, sessionHandler, TEST_ORIGIN } from "../../test/handlers";
import { CATEGORIES } from "../../test/review-fixtures";
import { server } from "../../test/server";

import { buildCategoryPatch, CategoryDrawer, type CategoryFormValues } from "./CategoryDrawer";

const URL_CATEGORIES = `${TEST_ORIGIN}/api/categories`;

function categoryById(id: number): CategoryModel {
  const found = CATEGORIES.find((category) => category.id === id);
  if (found === undefined) {
    throw new Error(`fixture sem categoria ${String(id)}`);
  }
  return found;
}
const MEME = categoryById(3);

interface Props {
  open?: boolean;
  category?: CategoryModel | null;
  onClose?: () => void;
}

function renderDrawer({ open = true, category = null, onClose = vi.fn() }: Props = {}): void {
  render(
    <QueryClientProvider client={createQueryClient(0)}>
      <App>
        <CategoryDrawer open={open} category={category} onClose={onClose} />
      </App>
    </QueryClientProvider>,
  );
}

function dialog(): HTMLElement {
  return screen.getByRole("dialog");
}

function field(label: string): HTMLElement {
  return within(dialog()).getByLabelText(label);
}

describe("buildCategoryPatch", () => {
  const saved = MEME;

  it("devolve só os campos alterados", () => {
    const values: CategoryFormValues = { ...toValues(saved), name: "Memes novos" };

    expect(buildCategoryPatch(values, saved)).toEqual({ name: "Memes novos" });
  });

  it("devolve objeto vazio quando nada mudou", () => {
    expect(buildCategoryPatch(toValues(saved), saved)).toEqual({});
  });

  it("inclui descrição e removível quando mudam", () => {
    const values: CategoryFormValues = {
      ...toValues(saved),
      description: "Outra descrição",
      removable: false,
    };

    expect(buildCategoryPatch(values, saved)).toEqual({
      description: "Outra descrição",
      removable: false,
    });
  });
});

describe("CategoryDrawer", () => {
  it("cria categoria com nome, descrição e removível, avisa e fecha", async () => {
    const bodies: unknown[] = [];
    const onClose = vi.fn();
    server.use(
      sessionHandler(),
      http.post(URL_CATEGORIES, async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json({ ...MEME, id: 9, name: "Recibos" }, { status: 201 });
      }),
    );
    renderDrawer({ onClose });
    const user = userEvent.setup();

    expect(within(dialog()).getByText("Nova categoria")).toBeInTheDocument();
    await user.type(field("Nome"), "Recibos");
    await user.type(field("Descrição"), "Fotos de recibos");
    await user.click(within(dialog()).getByRole("button", { name: "Salvar" }));

    await waitFor(() => {
      expect(onClose).toHaveBeenCalledTimes(1);
    });
    expect(bodies).toEqual([{ name: "Recibos", description: "Fotos de recibos", removable: true }]);
  });

  it("exige nome e descrição sem enviar requisição", async () => {
    const bodies: unknown[] = [];
    server.use(
      sessionHandler(),
      http.post(URL_CATEGORIES, async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json(MEME, { status: 201 });
      }),
    );
    renderDrawer();
    const user = userEvent.setup();

    await user.click(within(dialog()).getByRole("button", { name: "Salvar" }));

    expect(await within(dialog()).findByText("Informe o nome.")).toBeInTheDocument();
    expect(within(dialog()).getByText("Informe a descrição.")).toBeInTheDocument();
    expect(bodies).toEqual([]);
  });

  it("mostra o nome duplicado (409) no campo e mantém o Drawer aberto", async () => {
    const onClose = vi.fn();
    server.use(
      sessionHandler(),
      http.post(URL_CATEGORIES, () => errorResponse("CONFLICT", "duplicado", 409)),
    );
    renderDrawer({ onClose });
    const user = userEvent.setup();

    await user.type(field("Nome"), "Meme");
    await user.type(field("Descrição"), "Qualquer");
    await user.click(within(dialog()).getByRole("button", { name: "Salvar" }));

    expect(
      await within(dialog()).findByText("Já existe uma categoria com este nome."),
    ).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("preenche os campos ao editar e envia só o nome alterado", async () => {
    const calls: { id: string; body: unknown }[] = [];
    const onClose = vi.fn();
    server.use(
      sessionHandler(),
      http.patch(`${URL_CATEGORIES}/:id`, async ({ request, params }) => {
        calls.push({ id: String(params.id), body: await request.json() });
        return HttpResponse.json({ ...MEME, name: "Memes" });
      }),
    );
    renderDrawer({ category: MEME, onClose });
    const user = userEvent.setup();

    expect(within(dialog()).getByText("Editar categoria")).toBeInTheDocument();
    expect(field("Nome")).toHaveValue("Meme");
    expect(field("Descrição")).toHaveValue(MEME.description);
    await user.clear(field("Nome"));
    await user.type(field("Nome"), "Memes");
    await user.click(within(dialog()).getByRole("button", { name: "Salvar" }));

    await waitFor(() => {
      expect(onClose).toHaveBeenCalledTimes(1);
    });
    expect(calls).toEqual([{ id: String(MEME.id), body: { name: "Memes" } }]);
  });

  it("fecha sem requisição quando nada foi alterado", async () => {
    const calls: unknown[] = [];
    const onClose = vi.fn();
    server.use(
      sessionHandler(),
      http.patch(`${URL_CATEGORIES}/:id`, async ({ request }) => {
        calls.push(await request.json());
        return HttpResponse.json(MEME);
      }),
    );
    renderDrawer({ category: MEME, onClose });
    const user = userEvent.setup();

    await user.click(within(dialog()).getByRole("button", { name: "Salvar" }));

    await waitFor(() => {
      expect(onClose).toHaveBeenCalledTimes(1);
    });
    expect(calls).toEqual([]);
  });

  it("mostra mensagem para erro inesperado da API e mantém o Drawer aberto", async () => {
    const onClose = vi.fn();
    server.use(
      sessionHandler(),
      http.post(URL_CATEGORIES, () => errorResponse("INTERNAL", "x", 500)),
    );
    renderDrawer({ onClose });
    const user = userEvent.setup();

    await user.type(field("Nome"), "Recibos");
    await user.type(field("Descrição"), "Fotos");
    await user.click(within(dialog()).getByRole("button", { name: "Salvar" }));

    expect(
      await screen.findByText("Ocorreu um erro interno. Tente novamente."),
    ).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
  });
});

function toValues(category: CategoryModel): CategoryFormValues {
  return {
    name: category.name,
    description: category.description,
    removable: category.removable,
  };
}
