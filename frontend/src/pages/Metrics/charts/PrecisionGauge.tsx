// SPDX-License-Identifier: GPL-3.0-only
import type { ReactElement } from "react";
import { lazy } from "react";

import { ChartFrame } from "./ChartFrame";
import { PRECISION_GOAL } from "./PrecisionLineChart";

const Gauge = lazy(() =>
  import("@ant-design/charts").then((module) => ({ default: module.Gauge })),
);

/** Dados do medidor: precisão vigente contra a meta (faixas abaixo e acima de 0,98). */
export function gaugeData(precision: number) {
  return {
    target: precision,
    total: 1,
    name: "Precisão removível",
    thresholds: [PRECISION_GOAL, 1],
  };
}

export function PrecisionGauge({
  precision,
}: Readonly<{ precision: number | null }>): ReactElement {
  return (
    <ChartFrame label="Precisão do modelo vigente contra a meta" empty={precision === null}>
      {precision === null ? null : <Gauge data={gaugeData(precision)} height={240} />}
    </ChartFrame>
  );
}
