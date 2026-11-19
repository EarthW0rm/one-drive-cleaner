// SPDX-License-Identifier: GPL-3.0-only
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { reviewItem } from "../../test/review-fixtures";

import { confidencePercent, ThumbnailGrid, type ThumbnailGridProps } from "./ThumbnailGrid";

const NAMES: Record<number, string> = { 1: "Foto real", 2: "Print de tela" };

function setup(overrides: Partial<ThumbnailGridProps> = {}) {
  const props: ThumbnailGridProps = {
    items: [reviewItem(1), reviewItem(2)],
    categoryName: (id) => NAMES[id],
    urls: {
      "1": "https://thumbs.example.test/FAKE-1.jpg",
      "2": "https://thumbs.example.test/FAKE-2.jpg",
    },
    failed: new Set(),
    loading: false,
    selected: new Set(),
    focusedId: null,
    onToggle: vi.fn(),
    onFocusItem: vi.fn(),
    onImageError: vi.fn(),
    ...overrides,
  };
  render(<ThumbnailGrid {...props} />);
  return props;
}

describe("confidencePercent", () => {
  it.each([
    [0.456, 46],
    [1.7, 100],
    [-0.2, 0],
  ])("converte %s em %s%%", (value, expected) => {
    expect(confidencePercent(value)).toBe(expected);
  });
});

describe("ThumbnailGrid", () => {
  it("mostra miniatura, categoria sugerida e confiança de cada item", () => {
    setup();

    expect(screen.getByAltText("Miniatura do item 1")).toHaveAttribute(
      "src",
      "https://thumbs.example.test/FAKE-1.jpg",
    );
    expect(screen.getByText("Foto real")).toBeInTheDocument();
    expect(screen.getByText("Print de tela")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Confiança 30%" })).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
  });

  it("usa o id da categoria quando o nome é desconhecido", () => {
    setup({ items: [reviewItem(3)] });

    expect(screen.getByText("Categoria #3")).toBeInTheDocument();
  });

  it("mostra esqueletos durante o carregamento", () => {
    setup({ loading: true });

    expect(screen.getByLabelText("Carregando miniaturas")).toBeInTheDocument();
    expect(screen.queryByRole("list")).not.toBeInTheDocument();
  });

  it("mostra o estado vazio sem itens", () => {
    setup({ items: [] });

    expect(screen.getByText("Nenhum item para revisar")).toBeInTheDocument();
  });

  it("alterna a seleção pelo checkbox", async () => {
    const props = setup({ selected: new Set([2]) });

    await userEvent.click(screen.getByRole("checkbox", { name: "Selecionar item 1" }));

    expect(props.onToggle).toHaveBeenCalledWith(1);
    expect(screen.getByRole("checkbox", { name: "Selecionar item 2" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Selecionar item 1" })).not.toBeChecked();
  });

  it("avisa o foco do card", () => {
    const props = setup();

    fireEvent.focus(screen.getByLabelText(/^Item 2:/));

    expect(props.onFocusItem).toHaveBeenCalledWith(2);
  });

  it("mantém um único card na ordem de tabulação (o focado, ou o primeiro)", () => {
    setup({ focusedId: 2 });

    expect(screen.getByLabelText(/^Item 2:/)).toHaveAttribute("tabindex", "0");
    expect(screen.getByLabelText(/^Item 1:/)).toHaveAttribute("tabindex", "-1");
  });

  it("recai no primeiro card quando o focado não está na página", () => {
    setup({ focusedId: 99 });

    expect(screen.getByLabelText(/^Item 1:/)).toHaveAttribute("tabindex", "0");
  });

  it("avisa o erro de carregamento da imagem", () => {
    const props = setup();

    fireEvent.error(screen.getByAltText("Miniatura do item 2"));

    expect(props.onImageError).toHaveBeenCalledWith(2);
  });

  it("mostra placeholder para URL ausente e para imagem que falhou de vez", () => {
    setup({ urls: { "2": "https://thumbs.example.test/FAKE-2.jpg" }, failed: new Set([2]) });

    expect(screen.getAllByRole("img", { name: "Miniatura indisponível" })).toHaveLength(2);
    expect(screen.queryByAltText("Miniatura do item 2")).not.toBeInTheDocument();
  });

  it("renderiza uma página de 100 itens dentro do orçamento de 2 s (SLA da grade)", () => {
    const items = Array.from({ length: 100 }, (_, index) => reviewItem(index + 1));
    const urls = Object.fromEntries(
      items.map((item) => [
        String(item.item_id),
        `https://thumbs.example.test/FAKE-${String(item.item_id)}.jpg`,
      ]),
    );
    const started = performance.now();

    setup({ items, urls });

    expect(performance.now() - started).toBeLessThan(2000);
    expect(screen.getAllByRole("group")).toHaveLength(100);
  });
});
