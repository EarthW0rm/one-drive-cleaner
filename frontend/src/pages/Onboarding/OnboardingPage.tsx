// SPDX-License-Identifier: GPL-3.0-only
import { Alert, Button, Result, Skeleton, Space, Steps, Typography } from "antd";
import { useMemo, useState, type ReactElement } from "react";
import { useNavigate } from "react-router-dom";

import { useCompleteOnboarding, useScanRoots } from "../../api/folders";
import { DEFAULT_AUTOMATION_MODE, useSettings, type AutomationMode } from "../../api/settings";
import { errorMessage } from "../../app/errors";
import { FolderTreeSelect } from "../../components/FolderTreeSelect/FolderTreeSelect";

import { AutomationStep, MODE_OPTIONS } from "./AutomationStep";

const NEXT_PATH = "/varreduras";

type StepKey = "folders" | "automation" | "finish";

const STEP_TITLES: Record<StepKey, string> = {
  folders: "Pastas",
  automation: "Automação",
  finish: "Concluir",
};

export function OnboardingPage(): ReactElement {
  const navigate = useNavigate();
  const settings = useSettings();
  const roots = useScanRoots();
  const complete = useCompleteOnboarding();
  const [current, setCurrent] = useState(0);
  const [folderIds, setFolderIds] = useState<string[]>([]);
  const [mode, setMode] = useState<AutomationMode>(DEFAULT_AUTOMATION_MODE);

  const automationAvailable = settings.data != null;
  const steps: StepKey[] = automationAvailable
    ? ["folders", "automation", "finish"]
    : ["folders", "finish"];
  const step = steps[current] ?? "folders";
  const preselectPaths = useMemo(() => roots.data?.map((root) => root.display_path), [roots.data]);

  if (settings.isPending || roots.isPending) {
    return <Skeleton active aria-label="Carregando" />;
  }

  const modeLabel = MODE_OPTIONS.find((option) => option.value === mode)?.label ?? "";
  const summary = `${String(folderIds.length)} pasta(s) selecionada(s)${
    automationAvailable ? `; modo ${modeLabel}` : ""
  }. A primeira varredura começa ao concluir.`;
  const canAdvance = step !== "folders" || folderIds.length > 0;

  function finish(): void {
    complete.mutate(
      { folderIds, automationMode: automationAvailable ? mode : null },
      {
        onSuccess: () => {
          void navigate(NEXT_PATH, { replace: true });
        },
      },
    );
  }

  return (
    <Space orientation="vertical" size="large" style={{ width: "100%" }}>
      <Typography.Title level={2}>Primeira configuração</Typography.Title>
      <Steps current={current} items={steps.map((key) => ({ title: STEP_TITLES[key] }))} />
      {step === "folders" ? (
        <Space orientation="vertical" style={{ width: "100%" }}>
          <Typography.Paragraph>
            Escolha as pastas do OneDrive a analisar. As subpastas entram junto.
          </Typography.Paragraph>
          <FolderTreeSelect
            value={folderIds}
            onChange={setFolderIds}
            preselectPaths={preselectPaths}
          />
        </Space>
      ) : null}
      {step === "automation" ? <AutomationStep value={mode} onChange={setMode} /> : null}
      {step === "finish" ? <Result status="info" title="Tudo pronto" subTitle={summary} /> : null}
      {complete.isError ? (
        <Alert type="error" showIcon title={errorMessage(complete.error)} />
      ) : null}
      <Space>
        {current > 0 ? (
          <Button
            onClick={() => {
              setCurrent(current - 1);
            }}
          >
            Voltar
          </Button>
        ) : null}
        {step === "finish" ? (
          <Button type="primary" loading={complete.isPending} onClick={finish}>
            Concluir e iniciar varredura
          </Button>
        ) : (
          <Button
            type="primary"
            disabled={!canAdvance}
            onClick={() => {
              setCurrent(current + 1);
            }}
          >
            Avançar
          </Button>
        )}
      </Space>
    </Space>
  );
}
