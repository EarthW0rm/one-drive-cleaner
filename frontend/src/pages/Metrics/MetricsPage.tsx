// SPDX-License-Identifier: GPL-3.0-only
import {
  Alert,
  Button,
  Card,
  Col,
  Progress,
  Row,
  Skeleton,
  Space,
  Statistic,
  Typography,
} from "antd";
import type { ReactElement } from "react";

import { useCategories } from "../../api/categories";
import {
  METRICS_REFRESH_MS,
  useMetricsSummary,
  type AlertCode,
  type MetricsAlert,
  type MetricsSummary,
} from "../../api/metrics";
import { errorMessage } from "../../app/errors";
import { formatBytes, formatDateTime } from "../../app/format";

import { PrecisionGauge } from "./charts/PrecisionGauge";
import { PRECISION_GOAL, PrecisionLineChart } from "./charts/PrecisionLineChart";
import { QuarantinePieChart } from "./charts/QuarantinePieChart";
import { StateColumnChart } from "./charts/StateColumnChart";

const PERCENT = 100;
const DASH = "—";
const percentFormat = new Intl.NumberFormat("pt-BR", {
  style: "percent",
  maximumFractionDigits: 1,
});
const countFormat = new Intl.NumberFormat("pt-BR");

export const ALERT_TEXT: Record<AlertCode, (summary: MetricsSummary) => string> = {
  JOURNAL_PENDING: () =>
    "Há movimentações pendentes no diário local. Elas são concluídas ou revertidas na próxima reconciliação.",
  HIGH_FALSE_POSITIVE: () =>
    "Muitos itens movidos foram restaurados por você nos últimos 7 dias (falsos positivos). Considere subir o limiar de confiança nas Configurações.",
  TEMP_NEAR_CAP: () =>
    "O espaço temporário está perto do teto configurado. A análise pode pausar até a limpeza.",
  LOGIN_REQUIRED: () =>
    "É necessário entrar novamente na conta Microsoft para continuar a varredura e as movimentações.",
  DISK_OVER_BUDGET: (summary) =>
    `O uso de disco da aplicação passou de ${formatBytes(summary.disk.budget_bytes)}.`,
  AUTO_MOVE_CAPPED: () =>
    "A movimentação automática atingiu o teto por rodada (30% da quarentena ativa). O restante segue na próxima rodada.",
};

const FALLBACK_ALERT_TEXT = "Alerta desconhecido.";

function alertText(alert: MetricsAlert, summary: MetricsSummary): string {
  const build = (ALERT_TEXT as Partial<Record<string, (s: MetricsSummary) => string>>)[alert.code];
  return build?.(summary) ?? FALLBACK_ALERT_TEXT;
}

function rate(value: number | null): string {
  return value === null ? DASH : percentFormat.format(value);
}

function currentPrecision(summary: MetricsSummary): number | null {
  const current = summary.models.filter((model) => model.status === "current").at(-1);
  return current?.removable_precision ?? null;
}

function diskUsed(disk: MetricsSummary["disk"]): number {
  return disk.temp + disk.db + disk.model + disk.logs;
}

function Alerts({ summary }: Readonly<{ summary: MetricsSummary }>): ReactElement | null {
  if (summary.alerts.length === 0) {
    return null;
  }
  return (
    <Space orientation="vertical" style={{ width: "100%" }}>
      {summary.alerts.map((alert) => (
        <Alert
          key={alert.code}
          type={alert.code === "AUTO_MOVE_CAPPED" ? "info" : "warning"}
          showIcon
          title={alertText(alert, summary)}
          description={`Desde ${formatDateTime(alert.since)}`}
        />
      ))}
    </Space>
  );
}

function Statistics({ summary }: Readonly<{ summary: MetricsSummary }>): ReactElement {
  const items = Object.values(summary.by_state).reduce((sum, count) => sum + count, 0);
  const fp = summary.false_positive_rate;
  return (
    <Row gutter={[16, 16]}>
      <Col xs={12} md={6}>
        <Card>
          <Statistic title="Itens no acervo" value={countFormat.format(items)} />
        </Card>
      </Col>
      <Col xs={12} md={6}>
        <Card>
          <Statistic
            title="Itens em quarentena"
            value={countFormat.format(summary.quarantine.items)}
          />
        </Card>
      </Col>
      <Col xs={12} md={6}>
        <Card>
          <Statistic title="Volume liberável" value={formatBytes(summary.quarantine.bytes)} />
        </Card>
      </Col>
      <Col xs={12} md={6}>
        <Card>
          <Statistic
            title="Falsos positivos (7 dias)"
            value={rate(fp.last_7d)}
            suffix={fp.total === null ? undefined : `/ total ${rate(fp.total)}`}
          />
        </Card>
      </Col>
    </Row>
  );
}

function DiskUsage({ disk }: Readonly<{ disk: MetricsSummary["disk"] }>): ReactElement {
  const used = diskUsed(disk);
  const percent = disk.budget_bytes > 0 ? Math.round((used / disk.budget_bytes) * PERCENT) : 0;
  return (
    <Card title="Uso de disco">
      <Progress
        percent={Math.min(PERCENT, percent)}
        status={used > disk.budget_bytes ? "exception" : "normal"}
        format={() => `${formatBytes(used)} / ${formatBytes(disk.budget_bytes)}`}
        aria-label="Uso de disco contra o orçamento"
      />
      <Typography.Text type="secondary">
        Temporários {formatBytes(disk.temp)} · Banco {formatBytes(disk.db)} · Modelo{" "}
        {formatBytes(disk.model)} · Logs {formatBytes(disk.logs)}
      </Typography.Text>
    </Card>
  );
}

function MetricsContent({ summary }: Readonly<{ summary: MetricsSummary }>): ReactElement {
  const categories = useCategories();
  const categoryName = (categoryId: number): string | undefined =>
    categories.data?.find((category) => category.id === categoryId)?.name;
  return (
    <Space orientation="vertical" size="large" style={{ width: "100%" }}>
      <Alerts summary={summary} />
      <Statistics summary={summary} />
      <Row gutter={[16, 16]}>
        <Col xs={24} lg={12}>
          <Card title="Itens por estado">
            <StateColumnChart byState={summary.by_state} />
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card title="Quarentena por categoria">
            <QuarantinePieChart
              byCategory={summary.quarantine.by_category}
              categoryName={categoryName}
            />
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card title="Precisão removível por versão">
            <PrecisionLineChart models={summary.models} />
            <Typography.Text type="secondary">
              Linha tracejada: meta de {PRECISION_GOAL.toLocaleString("pt-BR")}.
            </Typography.Text>
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card title="Precisão do modelo vigente">
            <PrecisionGauge precision={currentPrecision(summary)} />
          </Card>
        </Col>
      </Row>
      <DiskUsage disk={summary.disk} />
    </Space>
  );
}

export function MetricsPage(): ReactElement {
  const metrics = useMetricsSummary(METRICS_REFRESH_MS);

  return (
    <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
      <Typography.Title level={2}>Métricas</Typography.Title>
      {metrics.isPending ? <Skeleton active aria-label="Carregando métricas" /> : null}
      {metrics.isError ? (
        <Alert
          type="error"
          showIcon
          title="Falha ao carregar as métricas"
          description={errorMessage(metrics.error)}
          action={
            <Button
              onClick={() => {
                void metrics.refetch();
              }}
            >
              Tentar novamente
            </Button>
          }
        />
      ) : null}
      {metrics.data === undefined ? null : <MetricsContent summary={metrics.data} />}
    </Space>
  );
}
