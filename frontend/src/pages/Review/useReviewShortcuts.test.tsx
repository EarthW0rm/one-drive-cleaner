// SPDX-License-Identifier: GPL-3.0-only
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { reviewItem } from "../../test/review-fixtures";

import { useReviewShortcuts, type ReviewShortcutsOptions } from "./useReviewShortcuts";

const ITEMS = [1, 2, 3, 4, 5, 6, 7].map((id) => reviewItem(id));

type Handlers = Pick<
  ReviewShortcutsOptions,
  "onFocusItem" | "onToggle" | "onSelectPage" | "onClearSelection" | "onConfirm" | "onClassify"
>;

function Harness(options: Readonly<ReviewShortcutsOptions>) {
  const [focusedId, setFocusedId] = useState(options.focusedId);
  const { containerRef, onKeyDown } = useReviewShortcuts({
    ...options,
    focusedId,
    onFocusItem: (itemId) => {
      setFocusedId(itemId);
      options.onFocusItem(itemId);
    },
  });
  return (
    // eslint-disable-next-line jsx-a11y/no-static-element-interactions -- harness do hook
    <div ref={containerRef} onKeyDown={onKeyDown}>
      {options.items.map((item) => (
        <div
          key={item.item_id}
          data-item-id={item.item_id}
          // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- card focável do harness
          tabIndex={0}
          aria-label={`card ${String(item.item_id)}`}
        >
          <input type="checkbox" aria-label={`check ${String(item.item_id)}`} />
        </div>
      ))}
    </div>
  );
}

function setup(overrides: Partial<ReviewShortcutsOptions> = {}): Handlers {
  const handlers: Handlers = {
    onFocusItem: vi.fn(),
    onToggle: vi.fn(),
    onSelectPage: vi.fn(),
    onClearSelection: vi.fn(),
    onConfirm: vi.fn(),
    onClassify: vi.fn(),
  };
  render(
    <Harness
      items={ITEMS}
      focusedId={3}
      selected={new Set()}
      categoryIds={[10, 20]}
      busy={false}
      {...handlers}
      {...overrides}
    />,
  );
  return handlers;
}

afterEach(() => {
  vi.restoreAllMocks();
});

function mockRows(columns: number): void {
  vi.spyOn(HTMLElement.prototype, "offsetTop", "get").mockImplementation(function (
    this: HTMLElement,
  ) {
    const id = Number(this.dataset.itemId ?? "1");
    return Math.floor((id - 1) / columns) * 100;
  });
}

describe("useReviewShortcuts: foco", () => {
  it("move o foco com as setas horizontais, em Home e em End", async () => {
    const handlers = setup();
    screen.getByLabelText("card 3").focus();

    await userEvent.keyboard("{ArrowRight}");
    expect(handlers.onFocusItem).toHaveBeenLastCalledWith(4);
    expect(screen.getByLabelText("card 4")).toHaveFocus();

    await userEvent.keyboard("{ArrowLeft}");
    expect(handlers.onFocusItem).toHaveBeenLastCalledWith(3);

    await userEvent.keyboard("{Home}");
    expect(handlers.onFocusItem).toHaveBeenLastCalledWith(1);

    await userEvent.keyboard("{End}");
    expect(handlers.onFocusItem).toHaveBeenLastCalledWith(7);
  });

  it("não passa dos limites da página", async () => {
    const handlers = setup({ focusedId: 1 });
    screen.getByLabelText("card 1").focus();

    await userEvent.keyboard("{ArrowLeft}");

    expect(handlers.onFocusItem).toHaveBeenLastCalledWith(1);
  });

  it("sobe e desce pela quantidade de colunas medida no layout", async () => {
    mockRows(3);
    const handlers = setup({ focusedId: 2 });
    screen.getByLabelText("card 2").focus();

    await userEvent.keyboard("{ArrowDown}");
    expect(handlers.onFocusItem).toHaveBeenLastCalledWith(5);

    await userEvent.keyboard("{ArrowUp}");
    expect(handlers.onFocusItem).toHaveBeenLastCalledWith(2);
  });

  it("começa do primeiro item quando nada está focado", async () => {
    const handlers = setup({ focusedId: null });
    screen.getByLabelText("card 1").focus();

    await userEvent.keyboard("{ArrowRight}");

    expect(handlers.onFocusItem).toHaveBeenLastCalledWith(2);
  });

  it("ignora as setas sem itens", async () => {
    const handlers = setup({ items: [], focusedId: null });

    await userEvent.keyboard("{ArrowRight}");

    expect(handlers.onFocusItem).not.toHaveBeenCalled();
  });
});

describe("useReviewShortcuts: ações", () => {
  it("alterna a seleção do focado com o espaço", async () => {
    const handlers = setup();
    screen.getByLabelText("card 3").focus();

    await userEvent.keyboard(" ");

    expect(handlers.onToggle).toHaveBeenCalledExactlyOnceWith(3);
  });

  it("deixa o espaço para o checkbox nativo", async () => {
    const handlers = setup();
    screen.getByLabelText("check 3").focus();

    await userEvent.keyboard(" ");

    expect(handlers.onToggle).not.toHaveBeenCalled();
  });

  it("confirma a sugestão do focado com Enter", async () => {
    const handlers = setup();
    screen.getByLabelText("card 3").focus();

    await userEvent.keyboard("{Enter}");

    expect(handlers.onConfirm).toHaveBeenCalledExactlyOnceWith([3]);
  });

  it("classifica o focado pelo número da categoria", async () => {
    const handlers = setup();
    screen.getByLabelText("card 3").focus();

    await userEvent.keyboard("2");

    expect(handlers.onClassify).toHaveBeenCalledExactlyOnceWith(20, [3]);
  });

  it("classifica a seleção quando há itens selecionados", async () => {
    const handlers = setup({ selected: new Set([1, 5]) });
    screen.getByLabelText("card 3").focus();

    await userEvent.keyboard("1");

    expect(handlers.onClassify).toHaveBeenCalledExactlyOnceWith(10, [1, 5]);
  });

  it("ignora número sem categoria correspondente e teclas sem atalho", async () => {
    const handlers = setup();
    screen.getByLabelText("card 3").focus();

    await userEvent.keyboard("9z");

    expect(handlers.onClassify).not.toHaveBeenCalled();
    expect(handlers.onConfirm).not.toHaveBeenCalled();
  });

  it("não grava enquanto há salvamento em andamento", async () => {
    const handlers = setup({ busy: true });
    screen.getByLabelText("card 3").focus();

    await userEvent.keyboard("{Enter}1");

    expect(handlers.onConfirm).not.toHaveBeenCalled();
    expect(handlers.onClassify).not.toHaveBeenCalled();
  });

  it("seleciona a página com A e limpa a seleção com Esc", async () => {
    const handlers = setup({ focusedId: null });
    screen.getByLabelText("card 1").focus();

    await userEvent.keyboard("a{Escape}");

    expect(handlers.onSelectPage).toHaveBeenCalledOnce();
    expect(handlers.onClearSelection).toHaveBeenCalledOnce();
  });

  it("sem item focado, espaço, Enter e números não fazem nada", async () => {
    const handlers = setup({ focusedId: null });
    screen.getByLabelText("card 1").focus();

    await userEvent.keyboard(" {Enter}1");

    expect(handlers.onToggle).not.toHaveBeenCalled();
    expect(handlers.onConfirm).not.toHaveBeenCalled();
    expect(handlers.onClassify).not.toHaveBeenCalled();
  });

  it("não intercepta combinações com Ctrl, Meta ou Alt", async () => {
    const handlers = setup();
    screen.getByLabelText("card 3").focus();

    await userEvent.keyboard("{Control>}a{/Control}{Meta>}1{/Meta}{Alt>}{Enter}{/Alt}");

    expect(handlers.onSelectPage).not.toHaveBeenCalled();
    expect(handlers.onClassify).not.toHaveBeenCalled();
    expect(handlers.onConfirm).not.toHaveBeenCalled();
  });
});
