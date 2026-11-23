// SPDX-License-Identifier: GPL-3.0-only
import { PictureOutlined } from "@ant-design/icons";
import {
  Card,
  Checkbox,
  Col,
  Empty,
  Image,
  Progress,
  Row,
  Skeleton,
  Tag,
  Typography,
  theme,
} from "antd";
import type { ReactElement, Ref } from "react";

import type { ReviewItem } from "../../api/review";

const SKELETON_CARDS = 8;
const THUMB_HEIGHT = 160;
const COLUMNS = { xs: 24, sm: 12, md: 8, lg: 6, xl: 4, xxl: 4 } as const;

export interface ThumbnailGridProps {
  readonly items: readonly ReviewItem[];
  /** Nome da categoria por id; `undefined` mostra "Categoria #id". */
  readonly categoryName: (categoryId: number) => string | undefined;
  readonly urls: Readonly<Record<string, string>>;
  readonly failed: ReadonlySet<number>;
  readonly loading: boolean;
  readonly selected: ReadonlySet<number>;
  readonly focusedId: number | null;
  readonly onToggle: (itemId: number) => void;
  readonly onFocusItem: (itemId: number) => void;
  readonly onImageError: (itemId: number) => void;
  readonly containerRef?: Ref<HTMLDivElement>;
  readonly onKeyDown?: (event: React.KeyboardEvent<HTMLDivElement>) => void;
}

export function confidencePercent(confidence: number): number {
  return Math.round(Math.min(1, Math.max(0, confidence)) * 100);
}

interface CardProps {
  readonly item: ReviewItem;
  readonly name: string;
  readonly url: string | undefined;
  readonly failed: boolean;
  readonly checked: boolean;
  readonly focusable: boolean;
  readonly onToggle: (itemId: number) => void;
  readonly onFocusItem: (itemId: number) => void;
  readonly onImageError: (itemId: number) => void;
}

export interface ThumbProps {
  readonly itemId: number;
  readonly url: string | undefined;
  readonly failed: boolean;
  readonly onImageError: (itemId: number) => void;
  /** Altura em px (padrão: a do card da grade). */
  readonly height?: number;
  readonly width?: number | string;
}

/** Miniatura com placeholder quando a URL falta ou falhou (reaproveitada pela Quarentena). */
export function Thumb({
  itemId,
  url,
  failed,
  onImageError,
  height = THUMB_HEIGHT,
  width = "100%",
}: Readonly<ThumbProps>): ReactElement {
  if (url === undefined || failed) {
    return (
      <div
        role="img"
        aria-label="Miniatura indisponível"
        style={{
          height,
          width,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          fontSize: Math.min(32, height / 2),
        }}
      >
        <PictureOutlined />
      </div>
    );
  }
  return (
    <Image
      src={url}
      alt={`Miniatura do item ${String(itemId)}`}
      height={height}
      width={width}
      style={{ objectFit: "cover" }}
      onError={() => {
        onImageError(itemId);
      }}
    />
  );
}

function ItemCard({
  item,
  name,
  url,
  failed,
  checked,
  focusable,
  ...handlers
}: CardProps): ReactElement {
  const { token } = theme.useToken();
  const percent = confidencePercent(item.confidence);
  const outline = checked ? `2px solid ${token.colorPrimary}` : "2px solid transparent";
  return (
    <Card
      size="small"
      hoverable
      role="group"
      data-item-id={item.item_id}
      tabIndex={focusable ? 0 : -1}
      aria-keyshortcuts="Enter Space"
      aria-label={`Item ${String(item.item_id)}: sugestão ${name}, confiança ${String(percent)}%`}
      style={{ outline, outlineOffset: -2 }}
      onFocus={() => {
        handlers.onFocusItem(item.item_id);
      }}
      cover={
        <Thumb
          itemId={item.item_id}
          url={url}
          failed={failed}
          onImageError={handlers.onImageError}
        />
      }
    >
      <div
        style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}
      >
        <Checkbox
          checked={checked}
          tabIndex={-1}
          aria-label={`Selecionar item ${String(item.item_id)}`}
          onChange={() => {
            handlers.onToggle(item.item_id);
          }}
        >
          <Tag color="blue">{name}</Tag>
        </Checkbox>
        <Progress
          type="circle"
          size={36}
          percent={percent}
          aria-label={`Confiança ${String(percent)}%`}
          format={(value) => String(value)}
        />
      </div>
    </Card>
  );
}

function SkeletonCards(): ReactElement {
  return (
    <Row gutter={[16, 16]} aria-busy="true" aria-label="Carregando miniaturas">
      {Array.from({ length: SKELETON_CARDS }, (_, index) => (
        <Col key={index} {...COLUMNS}>
          <Card size="small">
            <Skeleton.Image active style={{ width: "100%", height: THUMB_HEIGHT }} />
            <Skeleton active paragraph={false} />
          </Card>
        </Col>
      ))}
    </Row>
  );
}

/** Grade responsiva de miniaturas com sugestão, confiança e seleção múltipla. */
export function ThumbnailGrid({
  items,
  categoryName,
  urls,
  failed,
  loading,
  selected,
  focusedId,
  onToggle,
  onFocusItem,
  onImageError,
  containerRef,
  onKeyDown,
}: ThumbnailGridProps): ReactElement {
  if (loading) {
    return <SkeletonCards />;
  }
  if (items.length === 0) {
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description={<Typography.Text>Nenhum item para revisar</Typography.Text>}
      />
    );
  }
  const tabStop = items.some((item) => item.item_id === focusedId) ? focusedId : items[0]?.item_id;
  // Roving tabindex: os cards focáveis propagam o teclado ao contêiner.
  return (
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
    <div ref={containerRef} role="list" aria-label="Itens para revisão" onKeyDown={onKeyDown}>
      <Row gutter={[16, 16]}>
        {items.map((item) => (
          <Col key={item.item_id} role="listitem" {...COLUMNS}>
            <ItemCard
              item={item}
              name={
                categoryName(item.suggested_category_id) ??
                `Categoria #${String(item.suggested_category_id)}`
              }
              url={urls[String(item.item_id)]}
              failed={failed.has(item.item_id)}
              checked={selected.has(item.item_id)}
              focusable={item.item_id === tabStop}
              onToggle={onToggle}
              onFocusItem={onFocusItem}
              onImageError={onImageError}
            />
          </Col>
        ))}
      </Row>
    </div>
  );
}
