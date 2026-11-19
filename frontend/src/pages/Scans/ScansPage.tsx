// SPDX-License-Identifier: GPL-3.0-only
import {
  Alert,
  Button,
  Descriptions,
  Progress,
  Skeleton,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import type { ReactElement } from "react";

import { ApiError } from "../../api/client";
import {
  POLL_INTERVAL_MS,
  hasActiveJob,
  useJobs,
  usePauseJob,
  useResumeJob,
  useStartScan,
  type JobModel,
  type ScanMode,
} from "../../api/jobs";
import { useMetricsSummary } from "../../api/metrics";
import { errorMessage } from "../../app/errors";

import { FailuresList } from "./FailuresList";
import { StateCounters } from "./StateCounters";

export const PAUSE_REASON_TEXT: Record<string, string> = {
  login_required: "Pausado: é necessário entrar novamente na conta Microsoft.",
  temp_cap:
    "Pausado: o limite de espaço temporário foi atingido. Libere espaço ou aguarde a limpeza.",
  network: "Pausado: sem conexão com o OneDrive. Retoma sozinho quando a rede voltar.",
  user: "Pausado por você.",
};
const UNKNOWN_PAUSE_TEXT = "Pausado por um motivo desconhecido.";

const STATUS_LABEL: Record<string, string> = {
  queued: "Na fila",
  running: "Em andamento",
  paused: "Pausado",
  done: "Concluído",
  failed: "Falhou",
};
const STATUS_COLOR: Record<string, string> = {
  queued: "default",
  running: "processing",
  paused: "warning",
  done: "success",
  failed: "error",
};
const TYPE_LABEL: Record<string, string> = {
  scan: "Varredura",
  extract: "Extração",
  classify: "Classificação",
  train: "Treino",
  auto_actions: "Ações automáticas",
  move: "Movimentação",
  restore: "Restauração",
  delete: "Exclusão",
  reconcile: "Reconciliação",
  cleanup: "Limpeza",
};

function statusTag(status: string): ReactElement {
  return <Tag color={STATUS_COLOR[status] ?? "default"}>{STATUS_LABEL[status] ?? status}</Tag>;
}

function progressOf(job: JobModel): ReactElement {
  const { done, total } = job.progress;
  if (total === 0) {
    return <Typography.Text type="secondary">—</Typography.Text>;
  }
  return (
    <Progress
      percent={Math.round((done / total) * 100)}
      size="small"
      status={job.status === "running" ? "active" : "normal"}
    />
  );
}

function pauseTexts(jobs: readonly JobModel[]): string[] {
  const reasons = new Set<string>();
  for (const job of jobs) {
    if (job.status === "paused" && job.pause_reason !== null) {
      reasons.add(job.pause_reason);
    }
  }
  return [...reasons].map((reason) => PAUSE_REASON_TEXT[reason] ?? UNKNOWN_PAUSE_TEXT);
}

export function ScansPage(): ReactElement {
  const jobs = useJobs();
  const metrics = useMetricsSummary(hasActiveJob(jobs.data) ? POLL_INTERVAL_MS : false);
  const start = useStartScan();
  const pause = usePauseJob();
  const resume = useResumeJob();
  const [messageApi, contextHolder] = message.useMessage();

  function startScan(mode: ScanMode): void {
    start.mutate(mode, {
      onSuccess: () => void messageApi.success("Varredura iniciada."),
      onError: (error) => {
        if (error instanceof ApiError && error.code === "CONFLICT") {
          void messageApi.info("Varredura já em andamento");
        } else {
          void messageApi.error(errorMessage(error));
        }
      },
    });
  }

  if (jobs.isPending) {
    return <Skeleton active aria-label="Carregando" />;
  }

  const rows = jobs.data ?? [];
  const lastScan = rows.find((job) => job.type === "scan");
  const actionError = pause.error ?? resume.error;

  return (
    <Space orientation="vertical" size="large" style={{ width: "100%" }}>
      {contextHolder}
      <Typography.Title level={2}>Varreduras</Typography.Title>
      {jobs.pollingStopped ? (
        <Alert
          type="error"
          showIcon
          title="Não foi possível atualizar o andamento automaticamente."
          action={<Button onClick={jobs.restartPolling}>Tentar novamente</Button>}
        />
      ) : null}
      {jobs.isError && !jobs.pollingStopped ? (
        <Alert type="error" showIcon title={errorMessage(jobs.error)} />
      ) : null}
      {actionError === null ? null : (
        <Alert type="error" showIcon title={errorMessage(actionError)} />
      )}
      {pauseTexts(rows).map((text) => (
        <Alert key={text} type="warning" showIcon title={text} />
      ))}
      <Descriptions title="Última varredura" bordered size="small" column={1}>
        <Descriptions.Item label="Situação">
          {lastScan === undefined ? "Nenhuma varredura executada" : statusTag(lastScan.status)}
        </Descriptions.Item>
        {lastScan === undefined ? null : (
          <Descriptions.Item label="Pastas concluídas">
            {String(lastScan.progress.done)} de {String(lastScan.progress.total)}
          </Descriptions.Item>
        )}
      </Descriptions>
      {metrics.data === undefined ? null : <StateCounters summary={metrics.data} />}
      <Space>
        <Button
          type="primary"
          loading={start.isPending && start.variables === "auto"}
          onClick={() => {
            startScan("auto");
          }}
        >
          Varrer agora
        </Button>
        <Button
          loading={start.isPending && start.variables === "full"}
          onClick={() => {
            startScan("full");
          }}
        >
          Varredura completa
        </Button>
      </Space>
      <Table<JobModel>
        rowKey="id"
        dataSource={rows}
        pagination={false}
        locale={{ emptyText: "Nenhum job registrado" }}
        columns={[
          { title: "Tipo", dataIndex: "type", render: (type: string) => TYPE_LABEL[type] ?? type },
          { title: "Situação", dataIndex: "status", render: statusTag },
          { title: "Progresso", key: "progress", render: (_: unknown, job) => progressOf(job) },
          {
            title: "Erro",
            dataIndex: "error_code",
            render: (code: string | null) => code ?? "—",
          },
          {
            title: "Ações",
            key: "actions",
            render: (_: unknown, job) => {
              if (job.status === "running" || job.status === "queued") {
                return (
                  <Button
                    size="small"
                    onClick={() => {
                      pause.mutate(job.id);
                    }}
                  >
                    Pausar
                  </Button>
                );
              }
              if (job.status === "paused") {
                return (
                  <Button
                    size="small"
                    onClick={() => {
                      resume.mutate(job.id);
                    }}
                  >
                    Retomar
                  </Button>
                );
              }
              return null;
            },
          },
        ]}
      />
      <Typography.Title level={3}>Falhas de análise</Typography.Title>
      <FailuresList />
    </Space>
  );
}
