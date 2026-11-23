// SPDX-License-Identifier: GPL-3.0-only
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { NO_DATA_TEXT } from "./ChartFrame";
import { gaugeData, PrecisionGauge } from "./PrecisionGauge";

vi.mock("@ant-design/charts", () => ({
  Gauge: (props: object) => <div data-testid="gauge">{JSON.stringify(props)}</div>,
}));

describe("gaugeData", () => {
  it("compara a precisão com a meta de 0,98", () => {
    expect(gaugeData(0.95)).toMatchObject({ target: 0.95, total: 1, thresholds: [0.98, 1] });
  });
});

describe("PrecisionGauge", () => {
  it("passa a precisão vigente ao medidor", async () => {
    render(<PrecisionGauge precision={0.97} />);

    const chart = await screen.findByTestId("gauge");
    expect(JSON.parse(chart.textContent)).toMatchObject({ data: { target: 0.97 } });
  });

  it("mostra Empty quando a precisão é nula", () => {
    render(<PrecisionGauge precision={null} />);

    expect(screen.getByText(NO_DATA_TEXT)).toBeInTheDocument();
    expect(screen.queryByTestId("gauge")).not.toBeInTheDocument();
  });
});
