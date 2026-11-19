// SPDX-License-Identifier: GPL-3.0-only
import { App, Button, Drawer, Form, Input, Space, Switch } from "antd";
import { useEffect, type ReactElement } from "react";

import {
  useCreateCategory,
  useUpdateCategory,
  type CategoryModel,
  type CategoryPatch,
} from "../../api/categories";
import { ApiError } from "../../api/client";
import { errorMessage } from "../../app/errors";

const FORM_ID = "category-form";
const DUPLICATE_NAME_MESSAGE = "Já existe uma categoria com este nome.";
const NAME_MAX = 60;
const DESCRIPTION_MAX = 300;

export interface CategoryFormValues {
  name: string;
  description: string;
  removable: boolean;
}

const EMPTY_VALUES: CategoryFormValues = { name: "", description: "", removable: true };

function valuesFor(category: CategoryModel | null): CategoryFormValues {
  return category === null
    ? EMPTY_VALUES
    : { name: category.name, description: category.description, removable: category.removable };
}

/** Só os campos alterados em relação à categoria salva (o PATCH é parcial). */
export function buildCategoryPatch(
  values: CategoryFormValues,
  saved: CategoryModel,
): CategoryPatch {
  const patch: CategoryPatch = {};
  if (values.name !== saved.name) {
    patch.name = values.name;
  }
  if (values.description !== saved.description) {
    patch.description = values.description;
  }
  if (values.removable !== saved.removable) {
    patch.removable = values.removable;
  }
  return patch;
}

interface CategoryDrawerProps {
  open: boolean;
  /** `null` cria uma categoria nova; uma categoria abre a edição. */
  category: CategoryModel | null;
  onClose: () => void;
}

export function CategoryDrawer({
  open,
  category,
  onClose,
}: Readonly<CategoryDrawerProps>): ReactElement {
  const { message: messageApi } = App.useApp();
  const [form] = Form.useForm<CategoryFormValues>();
  const create = useCreateCategory();
  const update = useUpdateCategory();
  const saving = create.isPending || update.isPending;

  useEffect(() => {
    if (open) {
      form.resetFields();
      form.setFieldsValue(valuesFor(category));
    }
  }, [open, category, form]);

  const save = async (raw: CategoryFormValues): Promise<void> => {
    const values: CategoryFormValues = {
      name: raw.name.trim(),
      description: raw.description.trim(),
      removable: raw.removable,
    };
    try {
      if (category === null) {
        await create.mutateAsync(values);
        messageApi.success("Categoria criada.");
      } else {
        const patch = buildCategoryPatch(values, category);
        if (Object.keys(patch).length > 0) {
          await update.mutateAsync({ id: category.id, patch });
          messageApi.success("Categoria atualizada.");
        }
      }
      onClose();
    } catch (error) {
      if (error instanceof ApiError && error.code === "CONFLICT") {
        form.setFields([{ name: "name", errors: [DUPLICATE_NAME_MESSAGE] }]);
        return;
      }
      messageApi.error(errorMessage(error));
    }
  };

  return (
    <Drawer
      title={category === null ? "Nova categoria" : "Editar categoria"}
      open={open}
      onClose={onClose}
      footer={
        <Space style={{ float: "right" }}>
          <Button onClick={onClose}>Cancelar</Button>
          <Button type="primary" htmlType="submit" form={FORM_ID} loading={saving}>
            Salvar
          </Button>
        </Space>
      }
    >
      <Form<CategoryFormValues>
        id={FORM_ID}
        form={form}
        layout="vertical"
        onFinish={(values) => {
          void save(values);
        }}
      >
        <Form.Item
          name="name"
          label="Nome"
          rules={[
            { required: true, whitespace: true, message: "Informe o nome." },
            { max: NAME_MAX, message: `O nome pode ter até ${String(NAME_MAX)} caracteres.` },
          ]}
        >
          <Input autoComplete="off" />
        </Form.Item>
        <Form.Item
          name="description"
          label="Descrição"
          extra="Descreve o tipo de imagem; a partir dela o modelo passa a sugerir a categoria."
          rules={[
            { required: true, whitespace: true, message: "Informe a descrição." },
            {
              max: DESCRIPTION_MAX,
              message: `A descrição pode ter até ${String(DESCRIPTION_MAX)} caracteres.`,
            },
          ]}
        >
          <Input.TextArea rows={3} />
        </Form.Item>
        <Form.Item
          name="removable"
          label="Removível"
          valuePropName="checked"
          extra="Itens classificados nesta categoria podem ir para a quarentena."
        >
          <Switch aria-label="Removível" />
        </Form.Item>
      </Form>
    </Drawer>
  );
}
