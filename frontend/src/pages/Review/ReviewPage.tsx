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

import { CATEGORIES_KEY, useCategories } from "../../api/categories";
import { ApiError } from "../../api/client";
import { useSaveLabels, type LabelEntry, type LabelsResponse } from "../../api/labels";
import { labelsSinceLastTraining, useModels } from "../../api/models";
import {
  REVIEW_KEY,
  reviewKey,
  useReviewPage,
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

export function ReviewPage(): ReactElement {
  const { message: messageApi, notification } = App.useApp();
  const queryClient = useQueryClient();
  const [categoryId, setCategoryId] = useState<number | null>(null);
  const [order, setOrder] = useState<ReviewOrder>("uncertainty");
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [cursors, setCursors] = useState<readonly (string | null)[]>([null]);
  const [pageIndex, setPageIndex] = useState(0);
  const [selected, setSelected] = useState<ReadonlySet<number>>(new Set());
  const [focusedId, setFocusedId] = useState<number | null>(null);
  const [failedSave, setFailedSave] = useState<readonly LabelEntry[] | null>(null);

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
  const settings = useSettings();
  const saveLabels = useSaveLabels();
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
    const known = models.data === undefined ? null : labelsSinceLastTraining(models.data);
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
                onClick={() => {
                  setSelected(new Set());
                }}
              >
                Limpar seleção
              </Button>
            </Space>
          </Card>
        </Affix>
      ) : null}
    </Space>
  );
}
