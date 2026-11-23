// SPDX-License-Identifier: GPL-3.0-only
import { App, Alert, Button, Result, Select, Space, Statistic, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useMemo, useState, type ReactElement } from "react";

import { useCategories } from "../../api/categories";
import {
  MAX_RESTORES,
  useQuarantine,
  type QuarantineItem,
  type QuarantineQuery,
} from "../../api/quarantine";
import { errorMessage } from "../../app/errors";
import { formatBytes, formatDateTime } from "../../app/format";
import { DeleteConfirmModal } from "../../components/DeleteConfirmModal/DeleteConfirmModal";
import { Thumb } from "../../components/ThumbnailGrid/ThumbnailGrid";
import { useThumbnails } from "../../components/ThumbnailGrid/useThumbnails";

import { FallbackAlerts } from "./FallbackAlerts";
import { RestoreModal } from "./RestoreModal";

const PAGE_SIZE = 50;
const THUMB_SIZE = 56;
const MODE_LABEL: Record<string, string> = { manual: "Manual", auto: "Automático" };

function plural(count: number, one: string, many: string): string {
  return count === 1 ? one : many;
}

export function QuarantinePage(): ReactElement {
  const { message: messageApi } = App.useApp();
  const [categoryId, setCategoryId] = useState<number | null>(null);
  const [cursors, setCursors] = useState<readonly (string | null)[]>([null]);
  const [pageIndex, setPageIndex] = useState(0);
  const [selected, setSelected] = useState<ReadonlySet<number>>(new Set());
  const [restoring, setRestoring] = useState<readonly number[] | null>(null);
  const [deleting, setDeleting] = useState<readonly number[] | null>(null);

  const query: QuarantineQuery = {
    categoryId,
    cursor: cursors[pageIndex] ?? null,
    limit: PAGE_SIZE,
  };
  const quarantine = useQuarantine(query);
  const items = useMemo(() => quarantine.data?.items ?? [], [quarantine.data]);
  const thumbnails = useThumbnails(useMemo(() => items.map((item) => item.item_id), [items]));
  const categories = useCategories();

  const names = useMemo(
    () => new Map((categories.data ?? []).map((category) => [category.id, category.name])),
    [categories.data],
  );
  const activeCategories = useMemo(
    () => (categories.data ?? []).filter((category) => !category.archived),
    [categories.data],
  );
  const categoryName = (id: number): string | undefined => names.get(id);
  const labelOf = (id: number | null): string =>
    id === null ? "Sem categoria" : (categoryName(id) ?? `Categoria #${String(id)}`);

  const groups = quarantine.data?.groups ?? [];
  const nextCursor = quarantine.data?.next_cursor ?? null;
  const selectedIds = [...selected];
  const tooManyToRestore = selected.size > MAX_RESTORES;

  function resetPaging(): void {
    setCursors([null]);
    setPageIndex(0);
    setSelected(new Set());
  }

  function goTo(target: number): void {
    if (target > pageIndex && nextCursor !== null) {
      setCursors((current) => {
        const copy = [...current];
        copy[target] = nextCursor;
        return copy;
      });
    }
    setPageIndex(target);
  }

  const columns: ColumnsType<QuarantineItem> = [
    {
      title: "Miniatura",
      key: "thumbnail",
      width: THUMB_SIZE + 32,
      render: (_, item) => (
        <Thumb
          itemId={item.item_id}
          url={thumbnails.urls[String(item.item_id)]}
          failed={thumbnails.failed.has(item.item_id)}
          onImageError={thumbnails.onImageError}
          height={THUMB_SIZE}
          width={THUMB_SIZE}
        />
      ),
    },
    {
      title: "Categoria",
      key: "category",
      render: (_, item) => <Tag color="blue">{labelOf(item.category_id)}</Tag>,
    },
    {
      title: "Modo",
      dataIndex: "mode",
      render: (mode: string) => <Tag>{MODE_LABEL[mode] ?? mode}</Tag>,
    },
    {
      title: "Movido em",
      dataIndex: "moved_at",
      render: (movedAt: string | null) => formatDateTime(movedAt),
    },
    {
      title: "Tamanho",
      dataIndex: "size_bytes",
      render: (bytes: number) => formatBytes(bytes),
    },
  ];

  return (
    <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
      <Typography.Title level={2}>Quarentena</Typography.Title>
      <FallbackAlerts
        notices={quarantine.data?.restore_notices ?? []}
        categoryName={categoryName}
      />
      <Space size="large" wrap>
        <Statistic title="Itens" value={quarantine.data?.total_count ?? 0} />
        <Statistic title="Volume" value={formatBytes(quarantine.data?.total_bytes ?? 0)} />
      </Space>
      {groups.length === 0 ? null : (
        <Space wrap aria-label="Itens por categoria">
          {groups.map((group) => (
            <Tag key={group.category_id ?? "none"}>
              {labelOf(group.category_id)}: {group.count} · {formatBytes(group.total_bytes)}
            </Tag>
          ))}
        </Space>
      )}
      <Space wrap>
        <Select
          allowClear
          placeholder="Todas as categorias"
          aria-label="Filtrar por categoria"
          style={{ width: 260 }}
          value={categoryId}
          options={groups
            .filter((group) => group.category_id !== null)
            .map((group) => ({
              value: group.category_id,
              label: `${labelOf(group.category_id)} (${String(group.count)})`,
            }))}
          onChange={(value: number | null | undefined) => {
            setCategoryId(value ?? null);
            resetPaging();
          }}
        />
        <Button
          loading={quarantine.isFetching}
          onClick={() => {
            void quarantine.refetch();
          }}
        >
          Atualizar
        </Button>
        <Typography.Text type="secondary">
          {selected.size} {plural(selected.size, "selecionado", "selecionados")}
        </Typography.Text>
        <Button
          type="primary"
          disabled={selected.size === 0 || tooManyToRestore}
          onClick={() => {
            setRestoring(selectedIds);
          }}
        >
          Restaurar
        </Button>
        <Button
          danger
          disabled={selected.size === 0}
          onClick={() => {
            setDeleting(selectedIds);
          }}
        >
          Excluir
        </Button>
        <Button
          disabled={selected.size === 0}
          onClick={() => {
            setSelected(new Set());
          }}
        >
          Limpar seleção
        </Button>
      </Space>
      {tooManyToRestore ? (
        <Alert
          type="warning"
          showIcon
          title={`Restaure no máximo ${String(MAX_RESTORES)} itens por vez.`}
        />
      ) : null}
      {quarantine.isError ? (
        <Result
          status="error"
          title="Falha ao carregar a quarentena"
          subTitle={errorMessage(quarantine.error)}
          extra={
            <Button type="primary" onClick={() => void quarantine.refetch()}>
              Tentar novamente
            </Button>
          }
        />
      ) : (
        <Table<QuarantineItem>
          rowKey="item_id"
          size="middle"
          columns={columns}
          dataSource={[...items]}
          loading={quarantine.isPending}
          pagination={false}
          locale={{ emptyText: "A quarentena está vazia" }}
          rowSelection={{
            selectedRowKeys: selectedIds,
            preserveSelectedRowKeys: true,
            onChange: (keys) => {
              setSelected(new Set(keys.map(Number)));
            },
          }}
        />
      )}
      <Space style={{ justifyContent: "center", width: "100%" }}>
        <Button
          disabled={pageIndex === 0}
          onClick={() => {
            goTo(pageIndex - 1);
          }}
        >
          Anterior
        </Button>
        <Typography.Text>Página {pageIndex + 1}</Typography.Text>
        <Button
          disabled={nextCursor === null}
          onClick={() => {
            goTo(pageIndex + 1);
          }}
        >
          Próxima
        </Button>
      </Space>
      <RestoreModal
        open={restoring !== null}
        itemIds={restoring ?? []}
        categories={activeCategories}
        onClose={() => {
          setRestoring(null);
        }}
        onRestored={(count) => {
          setRestoring(null);
          setSelected(new Set());
          void messageApi.success(
            `${String(count)} ${plural(count, "item enfileirado", "itens enfileirados")} para restauração.`,
          );
        }}
      />
      <DeleteConfirmModal
        open={deleting !== null}
        itemIds={deleting ?? []}
        onClose={() => {
          setDeleting(null);
        }}
        onDeleted={(count) => {
          setDeleting(null);
          setSelected(new Set());
          void messageApi.success(
            `${String(count)} ${plural(count, "item segue", "itens seguem")} para a lixeira do OneDrive em segundo plano.`,
          );
        }}
      />
    </Space>
  );
}
