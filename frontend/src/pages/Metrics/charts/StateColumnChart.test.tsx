// SPDX-License-Identifier: GPL-3.0-only
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { NO_DATA_TEXT } from "./ChartFrame";
import { StateColumnChart, statePoints } from "./StateColumnChart";

vi.mock("@ant-design/charts", () => ({
  Column: (props: object) => <div data-testid="column">{JSON.stringify(props)}</div>,
}));

describe("statePoints", () => {
  it("ordena pelo ciclo de vida e põe estados desconhecidos no fim", () => {
    expect(statePoints({ SUGERIDO: 2, ZZZ: 1, DESCOBERTO: 5, AAA: 3 })).toEqual([
      { state: "Descoberto", count: 5 },
      { state: "Sugerido", count: 2 },
      { state: "AAA", count: 3 },
      { state: "ZZZ", count: 1 },
    ]);
  });
});

describe("StateColumnChart", () => {
  it("passa os dados transformados ao gráfico de colunas", async () => {
    render(<StateColumnChart byState={{ DESCOBERTO: 5, SUGERIDO: 2 }} />);

    const chart = await screen.findByTestId("column");
    expect(JSON.parse(chart.textContent)).toMatchObject({
      data: [
        { state: "Descoberto", count: 5 },
        { state: "Sugerido", count: 2 },
      ],
      xField: "state",
      yField: "count",
    });
  });

  it.each([null, {}])("mostra Empty sem dados (%j)", (byState) => {
    render(<StateColumnChart byState={byState} />);

    expect(screen.getByText(NO_DATA_TEXT)).toBeInTheDocument();
    expect(screen.queryByTestId("column")).not.toBeInTheDocument();
  });
});
