// SPDX-License-Identifier: GPL-3.0-only
import { Alert, Card, Col, Progress, Row, Skeleton, Space, Statistic, Tag, Typography } from "antd";
import type { ReactElement } from "react";

import { categoryPrecisions, type ModelsResponse } from "../../api/models";

const PERCENT = 100;
const decimal = new Intl.NumberFormat("pt-BR", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 3,
});
const percentage = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 1 });

export interface ReviewProgressProps {
  readonly models: ModelsResponse | undefined;
  readonly loading: boolean;
  readonly failed: boolean;
  /** Quantidade de rótulos novos que dispara o retreino (`retrain_every`). */
  readonly retrainEvery: number;
  /** Rótulos desde o último treino (`labels.since_last_training` do resumo); `null` = indisponível. */
  readonly labelsSinceTraining: number | null;
  readonly categoryName: (categoryId: number) => string | undefined;
}

function RetrainProgress({
  fresh,
  retrainEvery,
}: Readonly<{ fresh: number; retrainEvery: number }>): ReactElement {
  const percent = Math.min(PERCENT, Math.round((fresh / retrainEvery) * PERCENT));
  return (
    <div>
      <Typography.Text strong>Próximo retreino</Typography.Text>
      <Progress
        percent={percent}
        format={() => `${String(Math.min(fresh, retrainEvery))} / ${String(retrainEvery)}`}
        aria-label="Progresso até o próximo retreino"
      />
    </div>
  );
}

function GateStats({ models }: Readonly<{ models: ModelsResponse }>): ReactElement {
  const { gate } = models;
  return (
    <Space orientation="vertical" size="small" style={{ width: "100%" }}>
      <Space wrap>
        <Typography.Text strong>Meta do modo Automático</Typography.Text>
        <Tag color={gate.eligible ? "success" : "default"}>
          {gate.eligible ? "Meta atingida" : "Meta ainda não atingida"}
        </Tag>
      </Space>
      <Row gutter={16}>
        <Col span={8}>
          <Statistic title="Rótulos" value={gate.labels} suffix={`/ ${String(gate.labels_goal)}`} />
        </Col>
        <Col span={8}>
          <Statistic
            title="Precisão removível"
            value={gate.precision === null ? "—" : decimal.format(gate.precision)}
            suffix={`/ ${decimal.format(gate.precision_goal)}`}
          />
        </Col>
        <Col span={8}>
          <Statistic
            title="Amostras removíveis"
            value={gate.support}
            suffix={`/ ${String(gate.support_goal)}`}
          />
        </Col>
      </Row>
    </Space>
  );
}

function CategoryPrecisions({
  models,
  categoryName,
}: Readonly<{
  models: ModelsResponse;
  categoryName: (categoryId: number) => string | undefined;
}>): ReactElement | null {
  const scores = categoryPrecisions(models.current);
  if (scores.length === 0) {
    return null;
  }
  return (
    <div>
      <Typography.Text strong>Precisão por categoria</Typography.Text>
      <div>
        <Space wrap size={[4, 4]}>
          {scores.map((score) => (
            <Tag key={score.categoryId}>
              {categoryName(score.categoryId) ?? `Categoria #${String(score.categoryId)}`}:{" "}
              {percentage.format(score.precision * PERCENT)}% ({score.support})
            </Tag>
          ))}
        </Space>
      </div>
    </div>
  );
}

/** Progresso até o próximo retreino e distância até a meta do modo Automático (J3). */
export function ReviewProgress({
  models,
  loading,
  failed,
  retrainEvery,
  labelsSinceTraining,
  categoryName,
}: ReviewProgressProps): ReactElement {
  if (loading) {
    return (
      <div role="status" aria-label="Carregando estado do modelo">
        <Skeleton active paragraph={{ rows: 2 }} />
      </div>
    );
  }
  if (failed || models === undefined) {
    return <Alert type="warning" showIcon title="Não foi possível carregar o estado do modelo." />;
  }
  return (
    <Card size="small" aria-label="Estado do aprendizado">
      <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
        {labelsSinceTraining === null ? null : (
          <RetrainProgress fresh={labelsSinceTraining} retrainEvery={retrainEvery} />
        )}
        <GateStats models={models} />
        <CategoryPrecisions models={models} categoryName={categoryName} />
      </Space>
    </Card>
  );
}
