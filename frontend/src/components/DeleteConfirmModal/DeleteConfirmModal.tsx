// SPDX-License-Identifier: GPL-3.0-only
import { Alert, Button, Input, Modal, Skeleton, Space, Statistic, Typography } from "antd";
import { useEffect, useRef, useState, type ReactElement } from "react";

import { ApiError } from "../../api/client";
import { useConfirmDeletion, usePreviewDeletion } from "../../api/quarantine";
import { errorMessage } from "../../app/errors";
import { formatBytes } from "../../app/format";

const EXPIRED_TEXT =
  "A confirmação expirou. Confira a nova quantidade e o novo volume e confirme novamente.";

export interface DeleteConfirmModalProps {
  readonly open: boolean;
  /** Itens da quarentena a excluir; deve ser uma referência estável enquanto o modal estiver aberto. */
  readonly itemIds: readonly number[];
  readonly onClose: () => void;
  /** Chamado após o servidor aceitar a exclusão (os itens vão para a lixeira em segundo plano). */
  readonly onDeleted: (count: number) => void;
}

function DeleteConfirmDialog({
  itemIds,
  onClose,
  onDeleted,
}: Readonly<Omit<DeleteConfirmModalProps, "open">>): ReactElement {
  const preview = usePreviewDeletion();
  const confirm = useConfirmDeletion();
  const [typed, setTyped] = useState("");
  const [expired, setExpired] = useState(false);
  const [failure, setFailure] = useState<unknown>(null);
  const submitting = useRef(false);
  const { mutate: requestPreview } = preview;

  useEffect(() => {
    requestPreview(itemIds);
  }, [itemIds, requestPreview]);

  const data = preview.data;
  const loading = data === undefined && !preview.isError;
  const enabled = data !== undefined && typed.trim() === String(data.count) && !confirm.isPending;

  async function submit(): Promise<void> {
    if (data === undefined || submitting.current) {
      return;
    }
    submitting.current = true;
    setExpired(false);
    setFailure(null);
    try {
      await confirm.mutateAsync({ itemIds, confirmationToken: data.confirmation_token });
      onDeleted(data.count);
    } catch (error) {
      if (error instanceof ApiError && error.code === "INVALID_CONFIRMATION") {
        setTyped("");
        setExpired(true);
        requestPreview(itemIds);
      } else {
        setFailure(error);
      }
    } finally {
      submitting.current = false;
    }
  }

  return (
    <Modal
      open
      title="Enviar para a lixeira"
      onCancel={onClose}
      closable={!confirm.isPending}
      mask={{ closable: false }}
      footer={
        <Space>
          <Button onClick={onClose} disabled={confirm.isPending}>
            Cancelar
          </Button>
          <Button
            danger
            type="primary"
            disabled={!enabled}
            loading={confirm.isPending}
            onClick={() => {
              void submit();
            }}
          >
            Enviar para a lixeira
          </Button>
        </Space>
      }
    >
      <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
        {expired ? <Alert type="warning" showIcon title={EXPIRED_TEXT} /> : null}
        {failure === null ? null : <Alert type="error" showIcon title={errorMessage(failure)} />}
        {preview.isError ? (
          <Alert
            type="error"
            showIcon
            title={errorMessage(preview.error)}
            action={
              <Button
                size="small"
                onClick={() => {
                  requestPreview(itemIds);
                }}
              >
                Tentar novamente
              </Button>
            }
          />
        ) : null}
        {loading ? <Skeleton active aria-label="Calculando quantidade e volume" /> : null}
        {data === undefined ? null : (
          <>
            <Space size="large">
              <Statistic title="Itens a excluir" value={data.count} />
              <Statistic title="Volume a excluir" value={formatBytes(data.total_bytes)} />
            </Space>
            <Typography.Paragraph>
              Os itens vão para a lixeira do OneDrive e saem da quarentena. Para confirmar, digite a
              quantidade de itens: <strong>{data.count}</strong>.
            </Typography.Paragraph>
            <Input
              aria-label="Digite a quantidade de itens para confirmar"
              inputMode="numeric"
              autoComplete="off"
              value={typed}
              disabled={confirm.isPending}
              onChange={(event) => {
                setTyped(event.target.value);
              }}
              onPressEnter={() => {
                if (enabled) {
                  void submit();
                }
              }}
            />
          </>
        )}
      </Space>
    </Modal>
  );
}

/** Confirmação explícita da exclusão (RN-20): mostra quantidade e volume e exige digitar a quantidade. */
export function DeleteConfirmModal({
  open,
  ...rest
}: Readonly<DeleteConfirmModalProps>): ReactElement | null {
  // Desmontar ao fechar zera o campo digitado e a confirmação anterior a cada abertura.
  return open ? <DeleteConfirmDialog {...rest} /> : null;
}
