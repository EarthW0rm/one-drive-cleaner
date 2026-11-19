// SPDX-License-Identifier: GPL-3.0-only
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ChartFrame, NO_DATA_TEXT } from "./ChartFrame";

describe("ChartFrame", () => {
  it("mostra Empty e oculta o gráfico quando não há dados", () => {
    render(
      <ChartFrame label="Gráfico" empty>
        <p>conteúdo</p>
      </ChartFrame>,
    );

    expect(screen.getByText(NO_DATA_TEXT)).toBeInTheDocument();
    expect(screen.queryByText("conteúdo")).not.toBeInTheDocument();
  });

  it("renderiza o gráfico com descrição acessível", () => {
    render(
      <ChartFrame label="Gráfico" empty={false}>
        <p>conteúdo</p>
      </ChartFrame>,
    );

    expect(screen.getByRole("img", { name: "Gráfico" })).toBeInTheDocument();
    expect(screen.getByText("conteúdo")).toBeInTheDocument();
  });
});
