// SPDX-License-Identifier: GPL-3.0-only
import { Descriptions, Skeleton, Tag, Typography } from "antd";
import type { ReactElement } from "react";

import { useModels, type ModelsResponse } from "../../api/models";

const decimal = new Intl.NumberFormat("pt-BR", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 3,
});
const MODE_LABEL: Record<ModelsResponse["gate"]["effective_mode"], string> = {
  manual: "Manual",
  auto: "Automático",
};

function GateDetails({ gate }: Readonly<{ gate: ModelsResponse["gate"] }>): ReactElement {
  return (
    <Descriptions title="Estado atual do gate" size="small" bordered column={1}>
      <Descriptions.Item label="Precisão removível">
        {gate.precision === null ? "—" : decimal.format(gate.precision)} / meta{" "}
        {decimal.format(gate.precision_goal)}
      </Descriptions.Item>
      <Descriptions.Item label="Rótulos">
        {gate.labels} / meta {gate.labels_goal}
      </Descriptions.Item>
      <Descriptions.Item label="Amostras removíveis">
        {gate.support} / meta {gate.support_goal}
      </Descriptions.Item>
      <Descriptions.Item label="Elegível ao modo Automático">
        <Tag color={gate.eligible ? "success" : "default"}>{gate.eligible ? "Sim" : "Não"}</Tag>
      </Descriptions.Item>
      <Descriptions.Item label="Modo em vigor">{MODE_LABEL[gate.effective_mode]}</Descriptions.Item>
      {gate.threshold_matches ? null : (
        <Descriptions.Item label="Limiar">
          As métricas foram medidas em outro limiar; o gate reabre após a reavaliação do modelo.
        </Descriptions.Item>
      )}
    </Descriptions>
  );
}

/** Estado do gate do modo Evolutivo, lido de `GET /api/models`; falha não bloqueia a tela. */
export function GateState(): ReactElement {
  const models = useModels();
  if (models.isPending) {
    return <Skeleton active paragraph={{ rows: 2 }} aria-label="Carregando estado do gate" />;
  }
  if (models.isError) {
    return <Typography.Text type="secondary">Estado do gate indisponível.</Typography.Text>;
  }
  return <GateDetails gate={models.data.gate} />;
}
