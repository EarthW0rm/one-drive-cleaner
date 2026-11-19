// SPDX-License-Identifier: GPL-3.0-only
import { useQueryClient } from "@tanstack/react-query";
import {
  Affix,
  Alert,
  App,
  Button,
  Card,
  Pagination,
  Result,
  Segmented,
  Select,
  Space,
  Typography,
} from "antd";
import { useMemo, useState, type ReactElement } from "react";

import { CATEGORIES_KEY, useCategories, type CategoryModel } from "../../api/categories";
import { ApiError } from "../../api/client";
import { useSaveLabels, type LabelEntry, type LabelsResponse } from "../../api/labels";
import { useMetricsSummary } from "../../api/metrics";
import { useModels } from "../../api/models";
import { useMoveToQuarantine } from "../../api/quarantine";
import {
  REVIEW_KEY,
  reviewKey,
  useReviewPage,
  type ReviewItem,
  type ReviewOrder,
  type ReviewQuery,
  type ReviewResponse,
} from "../../api/review";
import { useSettings } from "../../api/settings";
import { errorMessage } from "../../app/errors";
import { ThumbnailGrid } from "../../components/ThumbnailGrid/ThumbnailGrid";
import { useThumbnails } from "../../components/ThumbnailGrid/useThumbnails";

import { ReviewProgress } from "./ReviewProgress";
import { useReviewShortcuts } from "./useReviewShortcuts";

const PAGE_SIZES = [25, 50, 100];
const DEFAULT_PAGE_SIZE = 50;
const DEFAULT_RETRAIN_EVERY = 20;
const MAX_DIGIT_SHORTCUTS = 9;
const NOTIFICATION_SECONDS = 8;
const ORDER_OPTIONS: { value: ReviewOrder; label: string }[] = [
  { value: "uncertainty", label: "Menor certeza primeiro" },
  { value: "confidence", label: "Maior confiança primeiro" },
];
const ARCHIVED_CATEGORY_TEXT =
  "A categoria escolhida foi arquivada ou não existe mais. A lista foi atualizada.";

function hasInvalidCategories(error: unknown): boolean {
  if (!(error instanceof ApiError) || error.code !== "VALIDATION_ERROR") {
    return false;
  }
  const details = error.details;
  return typeof details === "object" && details !== null && "invalid_category_ids" in details;
}

function plural(count: number, one: string, many: string): string {
  return count === 1 ? one : many;
}

/** IDs que o servidor recusou mover (`details.blocked_item_ids` do 422), se vierem no erro. */
function blockedItemIds(error: unknown): readonly number[] | null {
  if (!(error instanceof ApiError) || typeof error.details !== "object" || error.details === null) {
    return null;
  }
  const ids = (error.details as { blocked_item_ids?: unknown }).blocked_item_ids;
  return Array.isArray(ids) && ids.every((id) => typeof id === "number") ? ids : null;
}

const PROTECTED_MOVE_TEXT =
  "A seleção contém itens de Foto real (categoria protegida): eles nunca vão para a quarentena.";
const NOT_REMOVABLE_MOVE_TEXT =
  "A seleção contém itens de categoria não removível: só categorias removíveis vão para a quarentena.";
const CATEGORIES_MOVE_TEXT =
  "Lista de categorias indisponível: não é possível verificar se a seleção pode ir para a quarentena.";

/**
 * Motivo pelo qual a seleção não pode ir para a quarentena (RN-09), ou `null` se pode.
 * A categoria efetiva é o rótulo do utilizador, quando existe, senão a sugestão do modelo.
 */
function moveBlockReason(
  selectedItems: readonly ReviewItem[],
  categories: readonly CategoryModel[] | null | undefined,
): string | null {
  if (categories === null || categories === undefined) {
    return CATEGORIES_MOVE_TEXT;
  }
  const byId = new Map(categories.map((category) => [category.id, category]));
  let notRemovable = false;
  for (const item of selectedItems) {
    const category = byId.get(item.user_category_id ?? item.suggested_category_id);
    if (category?.protected === true) {
      return PROTECTED_MOVE_TEXT;
    }
    notRemovable ||= category?.removable !== true;
  }
  return notRemovable ? NOT_REMOVABLE_MOVE_TEXT : null;
}

export function ReviewPage(): ReactElement {
  const { message: messageApi, notification, modal } = App.useApp();
  const queryClient = useQueryClient();
  const [categoryId, setCategoryId] = useState<number | null>(null);
  const [order, setOrder] = useState<ReviewOrder>("uncertainty");
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [cursors, setCursors] = useState<readonly (string | null)[]>([null]);
  const [pageIndex, setPageIndex] = useState(0);
  const [selected, setSelected] = useState<ReadonlySet<number>>(new Set());
  const [focusedId, setFocusedId] = useState<number | null>(null);
  const [failedSave, setFailedSave] = useState<readonly LabelEntry[] | null>(null);
  const [blockedMove, setBlockedMove] = useState<readonly number[] | null>(null);

  const query: ReviewQuery = {
    categoryId,
    order,
    cursor: cursors[pageIndex] ?? null,
    limit: pageSize,
  };
  const review = useReviewPage(query);
  const items = useMemo(() => review.data?.items ?? [], [review.data]);
  const thumbnails = useThumbnails(useMemo(() => items.map((item) => item.item_id), [items]));
  const categories = useCategories();
  const models = useModels();
  const metrics = useMetricsSummary();
  const settings = useSettings();
  const saveLabels = useSaveLabels();
  const moveToQuarantine = useMoveToQuarantine();
  const retrainEvery = settings.data?.retrain_every ?? DEFAULT_RETRAIN_EVERY;

  const activeCategories = useMemo(
    () => (categories.data ?? []).filter((category) => !category.archived),
    [categories.data],
  );
  const names = useMemo(
    () => new Map((categories.data ?? []).map((category) => [category.id, category.name])),
    [categories.data],
  );
  const categoryName = (id: number): string | undefined => names.get(id);
  const options = activeCategories.map((category) => ({
    value: category.id,
    label: category.name,
  }));

  const pageIds = useMemo(() => new Set(items.map((item) => item.item_id)), [items]);
  const selectedOnPage = useMemo(
    () => new Set([...selected].filter((id) => pageIds.has(id))),
    [selected, pageIds],
  );

  const moveBlocked = useMemo(
    () =>
      moveBlockReason(
        items.filter((item) => selectedOnPage.has(item.item_id)),
        categories.data,
      ),
    [items, selectedOnPage, categories.data],
  );

  function resetPaging(): void {
    setCursors([null]);
    setPageIndex(0);
    setSelected(new Set());
    setFocusedId(null);
  }

  function goToPage(target: number, cursor: string | null = null): void {
    const next = cursor ?? review.data?.next_cursor ?? null;
    if (target > pageIndex && next !== null) {
      setCursors((current) => {
        const copy = [...current];
        copy[target] = next;
        return copy;
      });
    }
    setPageIndex(target);
    setSelected(new Set());
    setFocusedId(null);
  }

  function toggle(itemId: number): void {
    setSelected((current) => {
      const next = new Set(current);
      if (!next.delete(itemId)) {
        next.add(itemId);
      }
      return next;
    });
  }

  async function advanceIfEmpty(): Promise<void> {
    await queryClient.invalidateQueries({ queryKey: REVIEW_KEY });
    const fresh = queryClient.getQueryData<ReviewResponse>(reviewKey(query));
    if (fresh?.items.length !== 0) {
      return;
    }
    if (fresh.next_cursor !== null) {
      goToPage(pageIndex + 1, fresh.next_cursor);
    } else if (pageIndex > 0) {
      goToPage(pageIndex - 1);
    }
  }

  function announce(result: LabelsResponse): void {
    if (result.train_job_id !== null) {
      notification.info({
        title: "Retreino iniciado",
        description: `Você registrou ${String(retrainEvery)} novos rótulos. O modelo está sendo reavaliado em segundo plano.`,
        duration: NOTIFICATION_SECONDS,
      });
      return;
    }
    const known = metrics.data?.labels.since_last_training ?? null;
    const remaining = known === null ? null : Math.max(0, retrainEvery - known - result.saved);
    const base = `${String(result.saved)} ${plural(result.saved, "rótulo salvo", "rótulos salvos")}.`;
    void messageApi.success(
      remaining === null ? base : `${base} Faltam ${String(remaining)} para o próximo retreino.`,
    );
  }

  async function save(labels: readonly LabelEntry[]): Promise<void> {
    setFailedSave(null);
    try {
      const result = await saveLabels.mutateAsync(labels);
      setSelected(new Set());
      announce(result);
    } catch (error) {
      setFailedSave(labels);
      if (hasInvalidCategories(error)) {
        void queryClient.invalidateQueries({ queryKey: CATEGORIES_KEY });
      }
      void messageApi.error(
        hasInvalidCategories(error) ? ARCHIVED_CATEGORY_TEXT : errorMessage(error),
      );
      return;
    }
    await advanceIfEmpty();
  }

  async function moveSelected(itemIds: readonly number[]): Promise<void> {
    setBlockedMove(null);
    try {
      await moveToQuarantine.mutateAsync(itemIds);
    } catch (error) {
      setBlockedMove(blockedItemIds(error));
      void messageApi.error(errorMessage(error));
      return;
    }
    setSelected(new Set());
    void messageApi.success(
      `${String(itemIds.length)} ${plural(itemIds.length, "item enfileirado", "itens enfileirados")} para a quarentena. ${plural(itemIds.length, "Ele sai", "Eles saem")} da fila quando a movimentação terminar.`,
    );
  }

  function requestMove(itemIds: readonly number[]): void {
    const count = itemIds.length;
    modal.confirm({
      title: `Mover ${String(count)} ${plural(count, "item", "itens")} para a quarentena?`,
      content:
        "Os arquivos vão para a pasta Quarentena do OneDrive Cleaner e podem ser restaurados depois. Nada é excluído.",
      okText: "Mover para a quarentena",
      cancelText: "Cancelar",
      onOk: () => moveSelected(itemIds),
    });
  }

  function confirm(itemIds: readonly number[]): void {
    const labels = items
      .filter((item) => itemIds.includes(item.item_id))
      .map((item) => ({ item_id: item.item_id, category_id: item.suggested_category_id }));
    if (labels.length > 0) {
      void save(labels);
    }
  }

  function classify(category: number, itemIds: readonly number[]): void {
    const labels = itemIds
      .filter((id) => pageIds.has(id))
      .map((id) => ({ item_id: id, category_id: category }));
    if (labels.length > 0) {
      void save(labels);
    }
  }

  const shortcuts = useReviewShortcuts({
    items,
    focusedId,
    selected: selectedOnPage,
    categoryIds: activeCategories.slice(0, MAX_DIGIT_SHORTCUTS).map((category) => category.id),
    busy: saveLabels.isPending,
    onFocusItem: setFocusedId,
    onToggle: toggle,
    onSelectPage: () => {
      setSelected(new Set(pageIds));
    },
    onClearSelection: () => {
      setSelected(new Set());
    },
    onConfirm: confirm,
    onClassify: classify,
  });

  const nextCursor = review.data?.next_cursor ?? null;
  const total = (pageIndex + (nextCursor === null ? 1 : 2)) * pageSize;

  return (
    <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
      <Typography.Title level={2}>Revisão</Typography.Title>
      <ReviewProgress
        models={models.data}
        loading={models.isPending}
        failed={models.isError}
        retrainEvery={retrainEvery}
        labelsSinceTraining={metrics.data?.labels.since_last_training ?? null}
        categoryName={categoryName}
      />
      {categories.data === null || categories.isError ? (
        <Alert
          type="warning"
          showIcon
          title="Lista de categorias indisponível. Não é possível classificar até que ela carregue."
        />
      ) : null}
      <Space wrap>
        <Select
          allowClear
          showSearch={{ optionFilterProp: "label" }}
          placeholder="Todas as categorias"
          aria-label="Filtrar por categoria"
          style={{ width: 240 }}
          options={options}
          value={categoryId}
          onChange={(value: number | null | undefined) => {
            setCategoryId(value ?? null);
            resetPaging();
          }}
        />
        <Segmented
          aria-label="Ordem"
          options={ORDER_OPTIONS}
          value={order}
          onChange={(value) => {
            setOrder(value);
            resetPaging();
          }}
        />
        <Button
          aria-keyshortcuts="a"
          disabled={items.length === 0}
          onClick={() => {
            setSelected(new Set(pageIds));
          }}
        >
          Selecionar página
        </Button>
      </Space>
      <Typography.Text type="secondary">
        Atalhos na grade: setas movem o foco, Espaço seleciona, Enter confirma a sugestão, 1 a 9
        classificam na categoria de mesmo número, A seleciona a página e Esc limpa a seleção.
      </Typography.Text>
      {failedSave === null ? null : (
        <Alert
          type="error"
          showIcon
          title="Não foi possível salvar os rótulos. A seleção foi mantida."
          action={
            <Button
              size="small"
              loading={saveLabels.isPending}
              onClick={() => {
                void save(failedSave);
              }}
            >
              Tentar novamente
            </Button>
          }
        />
      )}
      {blockedMove === null ? null : (
        <Alert
          type="error"
          showIcon
          closable={{
            onClose: () => {
              setBlockedMove(null);
            },
          }}
          title={`O servidor bloqueou ${String(blockedMove.length)} ${plural(blockedMove.length, "item", "itens")} da movimentação: ${blockedMove.join(", ")}. Remova-os da seleção e tente de novo.`}
        />
      )}
      {review.isError ? (
        <Result
          status="error"
          title="Falha ao carregar a fila de revisão"
          subTitle={errorMessage(review.error)}
          extra={
            <Button type="primary" onClick={() => void review.refetch()}>
              Tentar novamente
            </Button>
          }
        />
      ) : (
        <ThumbnailGrid
          items={items}
          categoryName={categoryName}
          urls={thumbnails.urls}
          failed={thumbnails.failed}
          loading={review.isPending}
          selected={selectedOnPage}
          focusedId={focusedId}
          onToggle={toggle}
          onFocusItem={setFocusedId}
          onImageError={thumbnails.onImageError}
          containerRef={shortcuts.containerRef}
          onKeyDown={shortcuts.onKeyDown}
        />
      )}
      <Pagination
        align="center"
        current={pageIndex + 1}
        pageSize={pageSize}
        total={total}
        pageSizeOptions={PAGE_SIZES}
        showSizeChanger
        onChange={(page, size) => {
          if (size !== pageSize) {
            setPageSize(size);
            resetPaging();
            return;
          }
          goToPage(page - 1);
        }}
      />
      {selectedOnPage.size > 0 ? (
        <Affix offsetBottom={16}>
          <Card size="small" role="toolbar" aria-label="Ações em lote">
            <Space wrap>
              <Typography.Text>
                {selectedOnPage.size} {plural(selectedOnPage.size, "selecionado", "selecionados")}
              </Typography.Text>
              <Button
                type="primary"
                loading={saveLabels.isPending}
                onClick={() => {
                  confirm([...selectedOnPage]);
                }}
              >
                Confirmar sugestão
              </Button>
              <Select
                placeholder="Classificar como…"
                aria-label="Classificar como"
                style={{ width: 240 }}
                disabled={saveLabels.isPending}
                options={options}
                value={null}
                optionRender={(option, { index }) =>
                  index < MAX_DIGIT_SHORTCUTS ? (
                    <span>
                      {index + 1}. {option.label}
                    </span>
                  ) : (
                    option.label
                  )
                }
                onChange={(value: number) => {
                  classify(value, [...selectedOnPage]);
                }}
              />
              <Button
                disabled={moveBlocked !== null || moveToQuarantine.isPending}
                loading={moveToQuarantine.isPending}
                onClick={() => {
                  requestMove([...selectedOnPage]);
                }}
              >
                Mover para quarentena
              </Button>
              <Button
                onClick={() => {
                  setSelected(new Set());
                }}
              >
                Limpar seleção
              </Button>
            </Space>
            {moveBlocked === null ? null : (
              <Typography.Paragraph type="warning" role="status" style={{ margin: "8px 0 0" }}>
                {moveBlocked}
              </Typography.Paragraph>
            )}
          </Card>
        </Affix>
      ) : null}
    </Space>
  );
}
