// SPDX-License-Identifier: GPL-3.0-only
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { NO_DATA_TEXT } from "./ChartFrame";
import {
  goalAnnotation,
  PRECISION_GOAL,
  PrecisionLineChart,
  precisionPoints,
} from "./PrecisionLineChart";

vi.mock("@ant-design/charts", () => ({
  Line: (props: object) => <div data-testid="line">{JSON.stringify(props)}</div>,
}));

const MODELS = [
  { id: 3, removable_precision: 0.97 },
  { id: 2, removable_precision: null },
  { id: 1, removable_precision: 0.9 },
];

describe("precisionPoints", () => {
  it("ignora versões sem precisão e ordena da mais antiga para a mais nova", () => {
    expect(precisionPoints(MODELS)).toEqual([
      { version: "v1", precision: 0.9 },
      { version: "v3", precision: 0.97 },
    ]);
  });
});

describe("goalAnnotation", () => {
  it("desenha a linha horizontal na meta", () => {
    expect(goalAnnotation(0.98)).toMatchObject({ type: "lineY", data: [0.98] });
  });
});

describe("PrecisionLineChart", () => {
  it("inclui a linha de meta 0,98 na configuração do gráfico", async () => {
    render(<PrecisionLineChart models={MODELS} />);

    const chart = await screen.findByTestId("line");
    const config = JSON.parse(chart.textContent) as {
      data: unknown[];
      annotations: { type: string; data: number[] }[];
    };
    expect(config.data).toHaveLength(2);
    expect(config.annotations).toEqual([
      expect.objectContaining({ type: "lineY", data: [PRECISION_GOAL] }),
    ]);
  });

  it("mostra Empty quando nenhuma versão tem precisão medida", () => {
    render(<PrecisionLineChart models={[{ id: 1, removable_precision: null }]} />);

    expect(screen.getByText(NO_DATA_TEXT)).toBeInTheDocument();
    expect(screen.queryByTestId("line")).not.toBeInTheDocument();
  });
});
