// SPDX-License-Identifier: GPL-3.0-only
import type { ReactElement } from "react";
import { lazy } from "react";

import { ChartFrame } from "./ChartFrame";

const Column = lazy(() =>
  import("@ant-design/charts").then((module) => ({ default: module.Column })),
);

const STATE_LABEL: Record<string, string> = {
  DESCOBERTO: "Descoberto",
  ANALISADO: "Analisado",
  FALHA_ANALISE: "Falha na análise",
  SUGERIDO: "Sugerido",
  QUARENTENA: "Quarentena",
  RESTAURADO: "Restaurado",
  EXCLUIDO: "Excluído",
  REMOVIDO_EXTERNAMENTE: "Removido externamente",
};

export interface StatePoint {
  readonly state: string;
  readonly count: number;
}

/** Pontos na ordem do ciclo de vida; estados desconhecidos vão ao fim com o código cru. */
export function statePoints(byState: Readonly<Record<string, number>>): StatePoint[] {
  const known = Object.keys(STATE_LABEL);
  const keys = [
    ...known.filter((key) => key in byState),
    ...Object.keys(byState)
      .filter((key) => !known.includes(key))
      .sort((a, b) => a.localeCompare(b)),
  ];
  return keys.map((key) => ({ state: STATE_LABEL[key] ?? key, count: byState[key] ?? 0 }));
}

export function StateColumnChart({
  byState,
}: Readonly<{ byState: Readonly<Record<string, number>> | null }>): ReactElement {
  const data = byState === null ? [] : statePoints(byState);
  return (
    <ChartFrame label="Itens por estado" empty={data.length === 0}>
      <Column data={data} xField="state" yField="count" height={240} />
    </ChartFrame>
  );
}
