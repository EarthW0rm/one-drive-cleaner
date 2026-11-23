// SPDX-License-Identifier: GPL-3.0-only
import { useRef, type KeyboardEvent, type RefObject } from "react";

import type { ReviewItem } from "../../api/review";

const DIGIT = /^[1-9]$/;

export interface ReviewShortcutsOptions {
  readonly items: readonly ReviewItem[];
  readonly focusedId: number | null;
  readonly selected: ReadonlySet<number>;
  /** Categorias ativas na ordem das teclas `1..9`. */
  readonly categoryIds: readonly number[];
  /** Desativa as ações de gravação enquanto há um salvamento em andamento. */
  readonly busy: boolean;
  readonly onFocusItem: (itemId: number) => void;
  readonly onToggle: (itemId: number) => void;
  readonly onSelectPage: () => void;
  readonly onClearSelection: () => void;
  readonly onConfirm: (itemIds: readonly number[]) => void;
  readonly onClassify: (categoryId: number, itemIds: readonly number[]) => void;
}

export interface ReviewShortcuts {
  readonly containerRef: RefObject<HTMLDivElement | null>;
  readonly onKeyDown: (event: KeyboardEvent<HTMLDivElement>) => void;
}

/** Quantos cards cabem por linha: os que compartilham o `offsetTop` do primeiro. */
function columnsPerRow(container: HTMLElement): number {
  const cards = [...container.querySelectorAll<HTMLElement>("[data-item-id]")];
  const top = cards[0]?.offsetTop;
  const inFirstRow = cards.findIndex((card) => card.offsetTop !== top);
  return Math.max(1, inFirstRow === -1 ? cards.length : inFirstRow);
}

/**
 * Atalhos da grade: setas (foco), `Home`/`End`, espaço (seleciona), `Enter` (confirma a
 * sugestão do focado), `1..9` (classifica a seleção, ou o focado, na categoria de mesmo
 * índice), `a` (seleciona a página) e `Esc` (limpa a seleção). Só valem com o foco na grade.
 */
export function useReviewShortcuts(options: ReviewShortcutsOptions): ReviewShortcuts {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const { items, focusedId } = options;

  function moveFocus(target: number): void {
    const item = items[Math.min(items.length - 1, Math.max(0, target))];
    if (item === undefined) {
      return;
    }
    options.onFocusItem(item.item_id);
    containerRef.current
      ?.querySelector<HTMLElement>(`[data-item-id="${String(item.item_id)}"]`)
      ?.focus();
  }

  function move(key: string): boolean {
    const index = Math.max(
      0,
      items.findIndex((item) => item.item_id === focusedId),
    );
    const columns = containerRef.current === null ? 1 : columnsPerRow(containerRef.current);
    const targets: Record<string, number> = {
      ArrowRight: index + 1,
      ArrowLeft: index - 1,
      ArrowDown: index + columns,
      ArrowUp: index - columns,
      Home: 0,
      End: items.length - 1,
    };
    const target = targets[key];
    if (target === undefined) {
      return false;
    }
    moveFocus(target);
    return true;
  }

  function act(key: string): boolean {
    if (key === "a") {
      options.onSelectPage();
      return true;
    }
    if (key === "Escape") {
      options.onClearSelection();
      return true;
    }
    if (focusedId === null) {
      return false;
    }
    if (key === " ") {
      options.onToggle(focusedId);
      return true;
    }
    if (options.busy) {
      return key === "Enter" || DIGIT.test(key);
    }
    if (key === "Enter") {
      options.onConfirm([focusedId]);
      return true;
    }
    if (!DIGIT.test(key)) {
      return false;
    }
    const categoryId = options.categoryIds[Number(key) - 1];
    if (categoryId === undefined) {
      return false;
    }
    options.onClassify(categoryId, options.selected.size > 0 ? [...options.selected] : [focusedId]);
    return true;
  }

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    if (event.ctrlKey || event.metaKey || event.altKey) {
      return;
    }
    // O checkbox nativo já alterna sozinho com o espaço.
    if (event.key === " " && event.target instanceof HTMLInputElement) {
      return;
    }
    if (move(event.key) || act(event.key)) {
      event.preventDefault();
    }
  }

  return { containerRef, onKeyDown };
}
