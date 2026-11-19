// SPDX-License-Identifier: GPL-3.0-only
import {
  Alert,
  Button,
  Card,
  Col,
  Form,
  Input,
  InputNumber,
  Result,
  Row,
  Segmented,
  Skeleton,
  Slider,
  Space,
  Switch,
  Typography,
} from "antd";
import type { ReactElement } from "react";
import { useState } from "react";

import { ApiError } from "../../api/client";
import {
  isLoopbackEndpoint,
  useSettings,
  useUpdateSettings,
  type AutomationMode,
  type SettingsModel,
  type SettingsPatch,
} from "../../api/settings";
import { useLogout } from "../../api/useSession";
import { errorMessage } from "../../app/errors";

import { GateState } from "./GateState";

const MIB = 1024 * 1024;
const MIN_TEMP_CAP_MB = 256;
const THRESHOLD_LIMITS = { min: 0.5, max: 0.999 } as const;
const RETRAIN_LIMITS = { min: 5, max: 500 } as const;

const FORM_FIELDS = [
  "automation_mode",
  "auto_threshold",
  "retrain_every",
  "temp_cap_mb",
  "otel_enabled",
  "otel_endpoint",
] as const;
type FormField = (typeof FORM_FIELDS)[number];

/** Nome do campo na API quando diverge do nome do campo no formulário. */
const API_TO_FORM: Partial<Record<string, FormField>> = { temp_cap_bytes: "temp_cap_mb" };

const MODE_OPTIONS: { label: string; value: AutomationMode }[] = [
  { label: "Manual", value: "manual" },
  { label: "Automático", value: "auto" },
  { label: "Evolutivo", value: "evolutive" },
];

const MODE_DESCRIPTION: Record<AutomationMode, string> = {
  manual: "Nada é movido para a quarentena sem a sua confirmação.",
  auto: "Itens com confiança igual ou acima do limiar são movidos para a quarentena sozinhos.",
  evolutive:
    "Comporta-se como Manual até a precisão nas categorias removíveis chegar a 98% com pelo menos 200 classificações; depois, como Automático.",
};

const LOOPBACK_MESSAGE = "Use um endereço de loopback: 127.0.0.1, localhost ou ::1.";
const INVALID_FIELD_MESSAGE = "Valor inválido para este campo.";

export interface SettingsFormValues {
  automation_mode: AutomationMode;
  auto_threshold: number | null;
  retrain_every: number | null;
  temp_cap_mb: number | null;
  otel_enabled: boolean;
  otel_endpoint: string;
}

function toFormValues(settings: SettingsModel): SettingsFormValues {
  return {
    automation_mode: settings.automation_mode,
    auto_threshold: settings.auto_threshold,
    retrain_every: settings.retrain_every,
    temp_cap_mb: settings.temp_cap_bytes / MIB,
    otel_enabled: settings.otel_enabled,
    otel_endpoint: settings.otel_endpoint ?? "",
  };
}

/** Só os campos alterados em relação ao último estado salvo (a API recusa `null`). */
export function buildPatch(values: SettingsFormValues, saved: SettingsModel): SettingsPatch {
  const patch: SettingsPatch = {};
  if (values.automation_mode !== saved.automation_mode) {
    patch.automation_mode = values.automation_mode;
  }
  if (values.auto_threshold !== null && values.auto_threshold !== saved.auto_threshold) {
    patch.auto_threshold = values.auto_threshold;
  }
  if (values.retrain_every !== null && values.retrain_every !== saved.retrain_every) {
    patch.retrain_every = values.retrain_every;
  }
  if (values.temp_cap_mb !== null && values.temp_cap_mb * MIB !== saved.temp_cap_bytes) {
    patch.temp_cap_bytes = values.temp_cap_mb * MIB;
  }
  if (values.otel_enabled !== saved.otel_enabled) {
    patch.otel_enabled = values.otel_enabled;
  }
  if (values.otel_endpoint !== "" && values.otel_endpoint !== (saved.otel_endpoint ?? "")) {
    patch.otel_endpoint = values.otel_endpoint;
  }
  return patch;
}

function isFormField(value: unknown): value is FormField {
  return typeof value === "string" && (FORM_FIELDS as readonly string[]).includes(value);
}

/** Campos do formulário apontados pelo 422 (`details.fields[].loc`). */
function invalidFormFields(error: unknown): FormField[] {
  if (!(error instanceof ApiError) || error.status !== 422) {
    return [];
  }
  const details: unknown = error.details;
  if (typeof details !== "object" || details === null || !("fields" in details)) {
    return [];
  }
  const fields = Array.isArray(details.fields) ? (details.fields as unknown[]) : [];
  return fields.flatMap((item: unknown): FormField[] => {
    if (typeof item !== "object" || item === null || !("loc" in item)) {
      return [];
    }
    const loc = Array.isArray(item.loc) ? (item.loc as unknown[]) : [];
    const last = loc.at(-1);
    const name = typeof last === "string" ? (API_TO_FORM[last] ?? last) : undefined;
    return isFormField(name) ? [name] : [];
  });
}

interface RangeInputProps {
  value?: number | null;
  onChange?: (value: number | null) => void;
  min: number;
  max: number;
  step: number;
  precision: number;
  label: string;
}

/** Slider e InputNumber sincronizados; o InputNumber aceita digitação fora do slider. */
function RangeInput({
  value,
  onChange,
  min,
  max,
  step,
  precision,
  label,
}: Readonly<RangeInputProps>): ReactElement {
  return (
    <Row gutter={16} align="middle">
      <Col flex="auto">
        <Slider
          min={min}
          max={max}
          step={step}
          value={value ?? min}
          onChange={(next) => {
            if (typeof next === "number") {
              onChange?.(next);
            }
          }}
          tooltip={{ open: false }}
          aria-label={label}
        />
      </Col>
      <Col flex="140px">
        <InputNumber
          min={min}
          max={max}
          step={step}
          precision={precision}
          value={value ?? null}
          onChange={(next) => {
            onChange?.(next);
          }}
          aria-label={`${label} (valor numérico)`}
          style={{ width: "100%" }}
        />
      </Col>
    </Row>
  );
}

interface SettingsFormProps {
  saved: SettingsModel;
}

function SettingsForm({ saved }: Readonly<SettingsFormProps>): ReactElement {
  const [form] = Form.useForm<SettingsFormValues>();
  const update = useUpdateSettings();
  const logout = useLogout();
  const [status, setStatus] = useState<"saved" | "unchanged" | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [restartRequired, setRestartRequired] = useState(false);

  const save = async (values: SettingsFormValues): Promise<void> => {
    const patch = buildPatch(values, saved);
    if (Object.keys(patch).length === 0) {
      setStatus("unchanged");
      return;
    }
    setFormError(null);
    try {
      const result = await update.mutateAsync(patch);
      setRestartRequired((current) => current || result.restart_required);
      setStatus("saved");
    } catch (error) {
      const fields = invalidFormFields(error);
      form.setFields(fields.map((name) => ({ name, errors: [INVALID_FIELD_MESSAGE] })));
      setFormError(errorMessage(error));
    }
  };

  return (
    <Space orientation="vertical" size="large" style={{ width: "100%" }}>
      <Form<SettingsFormValues>
        form={form}
        name="settings"
        layout="vertical"
        initialValues={toFormValues(saved)}
        onFinish={(values) => {
          void save(values);
        }}
        onValuesChange={() => {
          setStatus(null);
        }}
      >
        <Card title="Automação">
          <Form.Item
            name="automation_mode"
            label="Modo de automação"
            extra={
              <Space orientation="vertical" size={0}>
                {MODE_OPTIONS.map((option) => (
                  <Typography.Text key={option.value} type="secondary">
                    {option.label}: {MODE_DESCRIPTION[option.value]}
                  </Typography.Text>
                ))}
              </Space>
            }
          >
            <Segmented options={MODE_OPTIONS} aria-label="Modo de automação" block />
          </Form.Item>
          <GateState />
          <Form.Item
            name="auto_threshold"
            label="Limiar de confiança para o modo Automático"
            rules={[
              { required: true, message: "Informe o limiar." },
              {
                type: "number",
                min: THRESHOLD_LIMITS.min,
                max: THRESHOLD_LIMITS.max,
                message: "Use um valor entre 0,5 e 0,999.",
              },
            ]}
          >
            <RangeInput
              min={THRESHOLD_LIMITS.min}
              max={THRESHOLD_LIMITS.max}
              step={0.001}
              precision={3}
              label="Limiar de confiança"
            />
          </Form.Item>
          <Form.Item
            name="retrain_every"
            label="Retreinar o modelo a cada N classificações do utilizador"
            rules={[
              { required: true, message: "Informe a frequência de retreino." },
              {
                type: "integer",
                min: RETRAIN_LIMITS.min,
                max: RETRAIN_LIMITS.max,
                message: "Use um número inteiro entre 5 e 500.",
              },
            ]}
          >
            <RangeInput
              min={RETRAIN_LIMITS.min}
              max={RETRAIN_LIMITS.max}
              step={1}
              precision={0}
              label="Frequência de retreino"
            />
          </Form.Item>
        </Card>

        <Card title="Espaço e telemetria" style={{ marginTop: 24 }}>
          <Form.Item
            name="temp_cap_mb"
            label="Teto de espaço temporário (MB)"
            rules={[
              { required: true, message: "Informe o teto de espaço temporário." },
              {
                type: "number",
                min: MIN_TEMP_CAP_MB,
                message: `Use no mínimo ${String(MIN_TEMP_CAP_MB)} MB.`,
              },
            ]}
          >
            <InputNumber min={MIN_TEMP_CAP_MB} precision={0} step={256} style={{ width: 200 }} />
          </Form.Item>
          <Form.Item
            name="otel_enabled"
            label="Telemetria local (OpenTelemetry)"
            valuePropName="checked"
          >
            <Switch aria-label="Telemetria local" />
          </Form.Item>
          <Form.Item
            name="otel_endpoint"
            label="Endereço do coletor de telemetria"
            extra="Somente endereços de loopback. A alteração vale após reiniciar a aplicação."
            rules={[
              {
                validator: (_rule, value: string) =>
                  (value === "" && saved.otel_endpoint === null) || isLoopbackEndpoint(value)
                    ? Promise.resolve()
                    : Promise.reject(new Error(LOOPBACK_MESSAGE)),
              },
            ]}
          >
            <Input placeholder="http://127.0.0.1:4317" />
          </Form.Item>
        </Card>

        {formError === null ? null : (
          <Alert type="error" showIcon title={formError} style={{ marginTop: 24 }} />
        )}
        {status === "saved" ? (
          <Alert type="success" showIcon title="Configurações salvas." style={{ marginTop: 24 }} />
        ) : null}
        {status === "unchanged" ? (
          <Alert
            type="info"
            showIcon
            title="Nenhuma alteração para salvar."
            style={{ marginTop: 24 }}
          />
        ) : null}
        {restartRequired ? (
          <Alert
            type="warning"
            showIcon
            title="Alterações de telemetria só valem após reiniciar a aplicação."
            style={{ marginTop: 24 }}
          />
        ) : null}

        <Form.Item style={{ marginTop: 24 }}>
          <Button type="primary" htmlType="submit" loading={update.isPending}>
            Salvar
          </Button>
        </Form.Item>
      </Form>

      <Card title="Conta">
        <Typography.Paragraph>
          Encerrar a sessão remove a conta Microsoft deste aplicativo neste computador.
        </Typography.Paragraph>
        <Button
          danger
          loading={logout.isPending}
          onClick={() => {
            logout.mutate();
          }}
        >
          Sair da conta
        </Button>
        {logout.isError ? (
          <Typography.Paragraph type="danger">{errorMessage(logout.error)}</Typography.Paragraph>
        ) : null}
      </Card>
    </Space>
  );
}

export function SettingsPage(): ReactElement {
  const settings = useSettings();

  if (settings.isPending) {
    return <Skeleton active />;
  }
  if (settings.isError) {
    return (
      <Result
        status="error"
        title="Falha ao carregar as configurações"
        subTitle={errorMessage(settings.error)}
      />
    );
  }
  if (settings.data === null) {
    return (
      <Result
        status="info"
        title="Configurações indisponíveis"
        subTitle="Esta versão da aplicação local não expõe as configurações."
      />
    );
  }
  return (
    <>
      <Typography.Title level={2}>Configurações</Typography.Title>
      <SettingsForm saved={settings.data} />
    </>
  );
}
