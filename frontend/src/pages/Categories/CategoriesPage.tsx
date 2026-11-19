// SPDX-License-Identifier: GPL-3.0-only
import { EditOutlined, PlusOutlined } from "@ant-design/icons";
import {
  Alert,
  App,
  Button,
  Flex,
  Popconfirm,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  type TableProps,
} from "antd";
import { useState, type ReactElement, type ReactNode } from "react";

import { useCategories, useUpdateCategory, type CategoryModel } from "../../api/categories";
import { errorMessage } from "../../app/errors";

import { CategoryDrawer } from "./CategoryDrawer";

const PROTECTED_TOOLTIP =
  "Foto real é protegida (RN-09): não pode ser renomeada, alterada nem arquivada.";
const UNAVAILABLE_TEXT = "Lista de categorias indisponível. Atualize a aplicação local.";

function yesNo(value: boolean): string {
  return value ? "Sim" : "Não";
}

/** Botão desabilitado não recebe eventos: o Tooltip fica no `span` que o envolve. */
function guarded(button: ReactNode, reason: string | null): ReactNode {
  return reason === null ? (
    button
  ) : (
    <Tooltip title={reason}>
      <span>{button}</span>
    </Tooltip>
  );
}

export function CategoriesPage(): ReactElement {
  const { message: messageApi } = App.useApp();
  const categories = useCategories();
  const update = useUpdateCategory();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editing, setEditing] = useState<CategoryModel | null>(null);

  function openCreate(): void {
    setEditing(null);
    setDrawerOpen(true);
  }

  function openEdit(category: CategoryModel): void {
    setEditing(category);
    setDrawerOpen(true);
  }

  async function archive(category: CategoryModel): Promise<void> {
    try {
      await update.mutateAsync({ id: category.id, patch: { archived: true } });
      messageApi.success(`Categoria "${category.name}" arquivada.`);
    } catch (error) {
      messageApi.error(errorMessage(error));
    }
  }

  const columns: TableProps<CategoryModel>["columns"] = [
    { title: "Nome", dataIndex: "name", key: "name" },
    {
      title: "Removível",
      dataIndex: "removable",
      key: "removable",
      render: (value: boolean) => yesNo(value),
    },
    {
      title: "Protegida",
      dataIndex: "protected",
      key: "protected",
      render: (value: boolean) => yesNo(value),
    },
    { title: "Rótulos", dataIndex: "labels", key: "labels" },
    {
      title: "Situação",
      dataIndex: "archived",
      key: "archived",
      render: (archived: boolean) =>
        archived ? <Tag>Arquivada</Tag> : <Tag color="green">Ativa</Tag>,
    },
    {
      title: "Ações",
      key: "actions",
      render: (_: unknown, category: CategoryModel) => {
        const editDisabled = category.protected;
        const archiveDisabled = category.protected || category.archived;
        const reason = category.protected ? PROTECTED_TOOLTIP : null;
        return (
          <Space>
            {guarded(
              <Button
                size="small"
                icon={<EditOutlined />}
                aria-label={`Editar ${category.name}`}
                disabled={editDisabled}
                onClick={() => {
                  openEdit(category);
                }}
              >
                Editar
              </Button>,
              reason,
            )}
            {guarded(
              <Popconfirm
                title="Arquivar a categoria?"
                description="Ela deixa de aparecer nas sugestões."
                okText="Arquivar"
                cancelText="Cancelar"
                disabled={archiveDisabled}
                onConfirm={() => {
                  void archive(category);
                }}
              >
                <Button
                  size="small"
                  danger
                  aria-label={`Arquivar ${category.name}`}
                  disabled={archiveDisabled}
                >
                  Arquivar
                </Button>
              </Popconfirm>,
              reason,
            )}
          </Space>
        );
      },
    },
  ];

  return (
    <Space orientation="vertical" size="large" style={{ width: "100%" }}>
      <Flex justify="space-between" align="center" wrap gap="middle">
        <Typography.Title level={2}>Categorias</Typography.Title>
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
          Nova categoria
        </Button>
      </Flex>
      {categories.data === null || categories.isError ? (
        <Alert type="warning" showIcon title={UNAVAILABLE_TEXT} />
      ) : null}
      <Table<CategoryModel>
        rowKey="id"
        columns={columns}
        dataSource={categories.data ?? []}
        loading={categories.isPending}
        pagination={false}
        scroll={{ x: true }}
      />
      <CategoryDrawer
        open={drawerOpen}
        category={editing}
        onClose={() => {
          setDrawerOpen(false);
        }}
      />
    </Space>
  );
}
