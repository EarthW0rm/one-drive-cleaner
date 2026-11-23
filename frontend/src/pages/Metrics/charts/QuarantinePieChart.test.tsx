// SPDX-License-Identifier: GPL-3.0-only
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { NO_DATA_TEXT } from "./ChartFrame";
import { categorySlices, QuarantinePieChart } from "./QuarantinePieChart";

vi.mock("@ant-design/charts", () => ({
  Pie: (props: object) => <div data-testid="pie">{JSON.stringify(props)}</div>,
}));

const NAMES: Record<number, string> = { 2: "Print de tela" };
const name = (id: number): string | undefined => NAMES[id];

describe("categorySlices", () => {
  it("resolve nomes, trata unclassified e descarta fatias vazias", () => {
    expect(categorySlices({ "2": 6, "9": 2, unclassified: 1, "3": 0 }, name)).toEqual([
      { category: "Print de tela", count: 6 },
      { category: "Categoria #9", count: 2 },
      { category: "Sem categoria", count: 1 },
    ]);
  });
});

describe("QuarantinePieChart", () => {
  it("passa as fatias ao gráfico de pizza", async () => {
    render(<QuarantinePieChart byCategory={{ "2": 6 }} categoryName={name} />);

    const chart = await screen.findByTestId("pie");
    expect(JSON.parse(chart.textContent)).toMatchObject({
      data: [{ category: "Print de tela", count: 6 }],
      angleField: "count",
      colorField: "category",
    });
  });

  it.each([null, {}])("mostra Empty sem dados (%j)", (byCategory) => {
    render(<QuarantinePieChart byCategory={byCategory} categoryName={name} />);

    expect(screen.getByText(NO_DATA_TEXT)).toBeInTheDocument();
  });
});
