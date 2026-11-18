// SPDX-License-Identifier: GPL-3.0-only
import { Alert, Button, Checkbox, Empty, List, Skeleton, Space, Typography, message } from "antd";
import { useState, type ReactElement } from "react";

import { useFailures, useRetryFailures } from "../../api/jobs";
import { errorMessage } from "../../app/errors";

export function FailuresList(): ReactElement {
  const failures = useFailures();
  const retry = useRetryFailures();
  const [messageApi, contextHolder] = message.useMessage();
  const [selected, setSelected] = useState<ReadonlySet<number>>(new Set());

  if (failures.isPending) {
    return <Skeleton active aria-label="Carregando falhas" />;
  }
  if (failures.isError) {
    return (
      <Alert
        type="error"
        showIcon
        title={errorMessage(failures.error)}
        action={<Button onClick={() => void failures.refetch()}>Tentar novamente</Button>}
      />
    );
  }

  const items = failures.data.pages.flatMap((page) => page.items);

  function toggle(itemId: number, checked: boolean): void {
    const next = new Set(selected);
    if (checked) {
      next.add(itemId);
    } else {
      next.delete(itemId);
    }
    setSelected(next);
  }

  function submit(): void {
    retry.mutate([...selected], {
      onSuccess: (result) => {
        setSelected(new Set());
        void messageApi.success(
          `${String(result.requeued)} item(ns) reenfileirado(s); ${String(result.skipped)} ignorado(s).`,
        );
      },
    });
  }

  return (
    <Space orientation="vertical" style={{ width: "100%" }}>
      {contextHolder}
      {retry.isError ? <Alert type="error" showIcon title={errorMessage(retry.error)} /> : null}
      {items.length === 0 ? (
        <Empty description="Nenhuma falha de análise" />
      ) : (
        <>
          <Button
            type="primary"
            disabled={selected.size === 0}
            loading={retry.isPending}
            onClick={submit}
          >
            Tentar novamente
          </Button>
          <List
            bordered
            dataSource={items}
            renderItem={(item) => (
              <List.Item key={item.item_id}>
                <Checkbox
                  checked={selected.has(item.item_id)}
                  onChange={(event) => {
                    toggle(item.item_id, event.target.checked);
                  }}
                  aria-label={`Selecionar item ${String(item.item_id)}`}
                >
                  <Typography.Text>Item #{item.item_id}</Typography.Text>
                </Checkbox>
                <Typography.Text type="secondary">
                  {item.failure_reason ?? "Motivo não informado"} · {item.failure_count}{" "}
                  {item.failure_count === 1 ? "tentativa" : "tentativas"}
                </Typography.Text>
              </List.Item>
            )}
          />
          {failures.hasNextPage ? (
            <Button
              loading={failures.isFetchingNextPage}
              onClick={() => void failures.fetchNextPage()}
            >
              Carregar mais
            </Button>
          ) : null}
        </>
      )}
    </Space>
  );
}
