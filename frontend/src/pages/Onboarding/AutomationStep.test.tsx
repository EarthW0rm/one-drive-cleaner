// SPDX-License-Identifier: GPL-3.0-only
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { AutomationStep } from "./AutomationStep";

describe("AutomationStep", () => {
  it("lista os três modos e descreve o selecionado", () => {
    render(<AutomationStep value="evolutive" onChange={() => undefined} />);

    for (const label of ["Manual", "Automático", "Evolutivo"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    expect(screen.getByText(/Começa manual e passa a agir sozinho/)).toBeInTheDocument();
  });

  it("notifica a troca de modo", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<AutomationStep value="evolutive" onChange={onChange} />);

    await user.click(screen.getByText("Manual"));

    expect(onChange).toHaveBeenCalledWith("manual");
  });

  it("não exibe descrição para um modo desconhecido", () => {
    // @ts-expect-error valor fora do contrato, só para cobrir o ramo defensivo
    render(<AutomationStep value="outro" onChange={() => undefined} />);

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
