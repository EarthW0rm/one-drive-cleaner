// SPDX-License-Identifier: GPL-3.0-only
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { restoreNotice } from "../../test/quarantine-fixtures";

import { FallbackAlerts } from "./FallbackAlerts";

const names = new Map([[1, "Foto real"]]);
const categoryName = (id: number): string | undefined => names.get(id);

describe("FallbackAlerts", () => {
  it("não renderiza nada sem avisos", () => {
    const { container } = render(<FallbackAlerts notices={[]} categoryName={categoryName} />);

    expect(container).toBeEmptyDOMElement();
  });

  it("não renderiza restaurações comuns, sem reclassificação nem fallback", () => {
    const { container } = render(
      <FallbackAlerts
        notices={[restoreNotice(1, { restore_reason: "false_positive" })]}
        categoryName={categoryName}
      />,
    );

    expect(container).toBeEmptyDOMElement();
  });

  it("avisa as restaurações automáticas após o retreino no singular e no plural", () => {
    const { rerender } = render(
      <FallbackAlerts notices={[restoreNotice(1)]} categoryName={categoryName} />,
    );
    expect(screen.getByText(/1 item foi restaurado automaticamente após o retreino/)).toBeVisible();
    expect(screen.getByText(/Item 1 \(Foto real\) em/)).toBeVisible();
    expect(screen.queryByText(/pasta Restaurados/)).not.toBeInTheDocument();

    rerender(
      <FallbackAlerts notices={[restoreNotice(1), restoreNotice(2)]} categoryName={categoryName} />,
    );

    expect(screen.getByText(/2 itens foram restaurados automaticamente/)).toBeVisible();
  });

  it("avisa o fallback para Restaurados (RN-19) no singular e no plural", () => {
    const fallback = { restore_reason: "false_positive", restore_fallback: true } as const;
    const { rerender } = render(
      <FallbackAlerts notices={[restoreNotice(5, fallback)]} categoryName={categoryName} />,
    );
    expect(screen.getByText(/1 item foi restaurado na pasta Restaurados/)).toBeVisible();
    expect(screen.queryByText(/automaticamente/)).not.toBeInTheDocument();

    rerender(
      <FallbackAlerts
        notices={[restoreNotice(5, fallback), restoreNotice(6, fallback)]}
        categoryName={categoryName}
      />,
    );

    expect(screen.getByText(/2 itens foram restaurados na pasta Restaurados/)).toBeVisible();
  });

  it("mostra categoria desconhecida ou ausente", () => {
    render(
      <FallbackAlerts
        notices={[restoreNotice(1, { category_id: 77 }), restoreNotice(2, { category_id: null })]}
        categoryName={categoryName}
      />,
    );

    expect(screen.getByText(/Item 1 \(Categoria #77\)/)).toBeVisible();
    expect(screen.getByText(/Item 2 \(sem categoria\)/)).toBeVisible();
  });
});
