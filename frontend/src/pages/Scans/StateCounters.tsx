// SPDX-License-Identifier: GPL-3.0-only
import { Card, Col, Row, Statistic } from "antd";
import type { ReactElement } from "react";

import type { MetricsSummary } from "../../api/metrics";
import { statePoints } from "../Metrics/charts/StateColumnChart";

const countFormat = new Intl.NumberFormat("pt-BR");

const BACKLOG_LABEL: Record<keyof MetricsSummary["backlog"], string> = {
  extract: "Aguardando extração",
  classify: "Aguardando classificação",
  review: "Aguardando revisão",
};

/** Contadores de itens por estado e backlog por etapa (spec J2, passo 5). */
export function StateCounters({ summary }: Readonly<{ summary: MetricsSummary }>): ReactElement {
  const points = statePoints(summary.by_state);
  return (
    <Card title="Itens por estado" size="small">
      <Row gutter={[16, 16]}>
        {points.map((point) => (
          <Col key={point.state} xs={12} md={6}>
            <Statistic title={point.state} value={countFormat.format(point.count)} />
          </Col>
        ))}
      </Row>
      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        {(Object.keys(BACKLOG_LABEL) as (keyof typeof BACKLOG_LABEL)[]).map((key) => (
          <Col key={key} xs={12} md={8}>
            <Statistic
              title={BACKLOG_LABEL[key]}
              value={countFormat.format(summary.backlog[key])}
            />
          </Col>
        ))}
      </Row>
    </Card>
  );
}
