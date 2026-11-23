// SPDX-License-Identifier: GPL-3.0-only
import { Alert, Segmented, Space, Typography } from "antd";
import type { ReactElement } from "react";

import type { AutomationMode } from "../../api/settings";

interface ModeOption {
  readonly value: AutomationMode;
  readonly label: string;
  readonly text: string;
}

export const MODE_OPTIONS: readonly ModeOption[] = [
  {
    value: "manual",
    label: "Manual",
    text: "Você revisa e confirma cada item; nada é movido para a quarentena sem a sua ação.",
  },
  {
    value: "auto",
    label: "Automático",
    text: "O modelo move para a quarentena os itens com alta confiança; o restante fica para a sua revisão.",
  },
  {
    value: "evolutive",
    label: "Evolutivo",
    text: "Começa manual e passa a agir sozinho quando a precisão medida nas suas revisões atinge a meta.",
  },
];

export interface AutomationStepProps {
  readonly value: AutomationMode;
  readonly onChange: (mode: AutomationMode) => void;
}

export function AutomationStep({ value, onChange }: AutomationStepProps): ReactElement {
  const current = MODE_OPTIONS.find((option) => option.value === value);
  return (
    <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
      <Typography.Paragraph>Escolha como a limpeza deve acontecer.</Typography.Paragraph>
      <Segmented<AutomationMode>
        aria-label="Modo de automação"
        options={MODE_OPTIONS.map(({ value: optionValue, label }) => ({
          value: optionValue,
          label,
        }))}
        value={value}
        onChange={onChange}
      />
      {current === undefined ? null : <Alert type="info" showIcon title={current.text} />}
    </Space>
  );
}
