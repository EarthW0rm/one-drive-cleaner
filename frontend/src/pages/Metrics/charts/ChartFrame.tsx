// SPDX-License-Identifier: GPL-3.0-only
import { Empty, Skeleton } from "antd";
import type { ReactElement, ReactNode } from "react";
import { Suspense } from "react";

export const NO_DATA_TEXT = "Sem dados para exibir";

export interface ChartFrameProps {
  /** Descrição acessível do gráfico. */
  readonly label: string;
  /** `true` oculta o gráfico e mostra `Empty` (métrica ausente ou sem pontos). */
  readonly empty: boolean;
  readonly children: ReactNode;
}

/** Moldura dos gráficos: carregamento lazy (`Suspense`) e estado vazio sem erro de renderização. */
export function ChartFrame({ label, empty, children }: Readonly<ChartFrameProps>): ReactElement {
  if (empty) {
    return <Empty description={NO_DATA_TEXT} image={Empty.PRESENTED_IMAGE_SIMPLE} />;
  }
  return (
    <div role="img" aria-label={label}>
      <Suspense fallback={<Skeleton.Node active style={{ width: "100%", height: 240 }} />}>
        {children}
      </Suspense>
    </div>
  );
}
