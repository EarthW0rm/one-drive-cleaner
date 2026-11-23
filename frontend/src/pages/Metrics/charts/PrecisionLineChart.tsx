// SPDX-License-Identifier: GPL-3.0-only
import type { ReactElement } from "react";
import { lazy } from "react";

import { ChartFrame } from "./ChartFrame";

const Line = lazy(() => import("@ant-design/charts").then((module) => ({ default: module.Line })));

export const PRECISION_GOAL = 0.98;

export interface ModelPoint {
  readonly id: number;
  readonly removable_precision: number | null;
}

export interface PrecisionPoint {
  readonly version: string;
  readonly precision: number;
}

/** Versões com precisão medida, da mais antiga para a mais nova. */
export function precisionPoints(models: readonly ModelPoint[]): PrecisionPoint[] {
  return models
    .flatMap((model) =>
      model.removable_precision === null
        ? []
        : [{ id: model.id, version: `v${String(model.id)}`, precision: model.removable_precision }],
    )
    .sort((a, b) => a.id - b.id)
    .map(({ version, precision }) => ({ version, precision }));
}

/** Linha de meta (0,98) desenhada como anotação horizontal tracejada. */
export function goalAnnotation(goal: number) {
  return {
    type: "lineY",
    data: [goal],
    style: { stroke: "#cf1322", lineDash: [4, 4] },
    labels: [{ text: `Meta ${goal.toLocaleString("pt-BR")}`, position: "right" }],
  };
}

export function PrecisionLineChart({
  models,
}: Readonly<{ models: readonly ModelPoint[] }>): ReactElement {
  const data = precisionPoints(models);
  return (
    <ChartFrame label="Precisão removível por versão do modelo" empty={data.length === 0}>
      <Line
        data={data}
        xField="version"
        yField="precision"
        height={240}
        point={{ shapeField: "circle", sizeField: 3 }}
        scale={{ y: { domainMin: 0, domainMax: 1 } }}
        annotations={[goalAnnotation(PRECISION_GOAL)]}
      />
    </ChartFrame>
  );
}
