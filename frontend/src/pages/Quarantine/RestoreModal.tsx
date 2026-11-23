// SPDX-License-Identifier: GPL-3.0-only
import { useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Form, Modal, Select, Space, Typography } from "antd";
import { useState, type ReactElement } from "react";

import { CATEGORIES_KEY, type CategoryModel } from "../../api/categories";
import { ApiError } from "../../api/client";
import { useRestoreFromQuarantine } from "../../api/quarantine";
import { errorMessage } from "../../app/errors";

const FORM_ID = "restore-form";
const REQUIRED_TEXT = "Escolha a categoria correta para restaurar.";
const ARCHIVED_TEXT =
  "A categoria escolhida foi arquivada ou não existe mais. A lista de categorias foi atualizada.";

interface RestoreFormValues {
  categoryId: number;
}

export interface RestoreModalProps {
  readonly open: boolean;
  /** Itens da quarentena marcados como indevidos. */
  readonly itemIds: readonly number[];
  /** Categorias ativas (a restauração vira rótulo do utilizador, então inclui as não removíveis). */
  readonly categories: readonly CategoryModel[];
  readonly onClose: () => void;
  readonly onRestored: (count: number) => void;
}

function RestoreDialog({
  itemIds,
  categories,
  onClose,
  onRestored,
}: Readonly<Omit<RestoreModalProps, "open">>): ReactElement {
  const queryClient = useQueryClient();
  const restore = useRestoreFromQuarantine();
  const [failure, setFailure] = useState<string | null>(null);
  const count = itemIds.length;

  async function submit(values: RestoreFormValues): Promise<void> {
    setFailure(null);
    try {
      await restore.mutateAsync(
        itemIds.map((itemId) => ({ item_id: itemId, category_id: values.categoryId })),
      );
      onRestored(count);
    } catch (error) {
      const archived = error instanceof ApiError && error.code === "VALIDATION_ERROR";
      if (archived) {
        void queryClient.invalidateQueries({ queryKey: CATEGORIES_KEY });
      }
      setFailure(archived ? ARCHIVED_TEXT : errorMessage(error));
    }
  }

  return (
    <Modal
      open
      title="Restaurar da quarentena"
      onCancel={onClose}
      mask={{ closable: false }}
      footer={
        <Space>
          <Button onClick={onClose}>Cancelar</Button>
          <Button type="primary" htmlType="submit" form={FORM_ID} loading={restore.isPending}>
            Restaurar
          </Button>
        </Space>
      }
    >
      <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
        <Typography.Paragraph>
          {count} {count === 1 ? "item volta" : "itens voltam"} à pasta e ao nome originais. A
          categoria escolhida é registrada como rótulo e o modelo aprende com a correção.
        </Typography.Paragraph>
        {failure === null ? null : <Alert type="error" showIcon title={failure} />}
        <Form<RestoreFormValues>
          id={FORM_ID}
          layout="vertical"
          onFinish={(values) => {
            void submit(values);
          }}
        >
          <Form.Item<RestoreFormValues>
            name="categoryId"
            label="Categoria correta"
            rules={[{ required: true, message: REQUIRED_TEXT }]}
          >
            <Select
              placeholder="Escolha a categoria"
              aria-label="Categoria correta"
              showSearch={{ optionFilterProp: "label" }}
              options={categories.map((category) => ({
                value: category.id,
                label: category.name,
              }))}
            />
          </Form.Item>
        </Form>
      </Space>
    </Modal>
  );
}

/** Restauração com escolha obrigatória da categoria correta (J5; RN-11, RN-19). */
export function RestoreModal({ open, ...rest }: Readonly<RestoreModalProps>): ReactElement | null {
  return open ? <RestoreDialog {...rest} /> : null;
}
