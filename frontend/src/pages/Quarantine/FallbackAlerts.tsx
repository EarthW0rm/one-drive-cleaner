// SPDX-License-Identifier: GPL-3.0-only
import { Alert, Typography } from "antd";
import type { ReactElement } from "react";

import type { RestoreNotice } from "../../api/quarantine";
import { formatDateTime } from "../../app/format";

const RECLASSIFIED = "reclassified";

export interface FallbackAlertsProps {
  readonly notices: readonly RestoreNotice[];
  /** Nome da categoria por id; `undefined` mostra "Categoria #id". */
  readonly categoryName: (categoryId: number) => string | undefined;
}

function line(notice: RestoreNotice, categoryName: FallbackAlertsProps["categoryName"]): string {
  const category =
    notice.category_id === null
      ? "sem categoria"
      : (categoryName(notice.category_id) ?? `Categoria #${String(notice.category_id)}`);
  return `Item ${String(notice.item_id)} (${category}) em ${formatDateTime(notice.restored_at)}`;
}

function NoticeList({ notices, categoryName }: Readonly<FallbackAlertsProps>): ReactElement {
  return (
    <ul style={{ margin: 0, paddingInlineStart: 20 }}>
      {notices.map((notice) => (
        <li key={notice.item_id}>
          <Typography.Text>{line(notice, categoryName)}</Typography.Text>
        </li>
      ))}
    </ul>
  );
}

/**
 * Avisos de restauração: reclassificação automática após o retreino (RN-17) e fallback para
 * `Restaurados` quando a pasta original não pôde ser usada (RN-19).
 */
export function FallbackAlerts({
  notices,
  categoryName,
}: Readonly<FallbackAlertsProps>): ReactElement | null {
  const automatic = notices.filter((notice) => notice.restore_reason === RECLASSIFIED);
  const fallback = notices.filter((notice) => notice.restore_fallback);
  if (automatic.length === 0 && fallback.length === 0) {
    return null;
  }
  return (
    <>
      {automatic.length === 0 ? null : (
        <Alert
          type="info"
          showIcon
          title={`${String(automatic.length)} ${automatic.length === 1 ? "item foi restaurado automaticamente" : "itens foram restaurados automaticamente"} após o retreino`}
          description={
            <>
              <Typography.Paragraph>
                O modelo reclassificou {automatic.length === 1 ? "o item" : "os itens"} para uma
                categoria que não pode ir para a quarentena.
              </Typography.Paragraph>
              <NoticeList notices={automatic} categoryName={categoryName} />
            </>
          }
        />
      )}
      {fallback.length === 0 ? null : (
        <Alert
          type="warning"
          showIcon
          title={`${String(fallback.length)} ${fallback.length === 1 ? "item foi restaurado" : "itens foram restaurados"} na pasta Restaurados`}
          description={
            <>
              <Typography.Paragraph>
                A pasta original não estava disponível. Os arquivos estão em /OneDrive
                Cleaner/Restaurados.
              </Typography.Paragraph>
              <NoticeList notices={fallback} categoryName={categoryName} />
            </>
          }
        />
      )}
    </>
  );
}
