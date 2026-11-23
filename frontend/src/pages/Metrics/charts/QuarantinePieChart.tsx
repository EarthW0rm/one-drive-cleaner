// SPDX-License-Identifier: GPL-3.0-only
import type { ReactElement } from "react";
import { lazy } from "react";

import { ChartFrame } from "./ChartFrame";

const Pie = lazy(() => import("@ant-design/charts").then((module) => ({ default: module.Pie })));

const UNCLASSIFIED_KEY = "unclassified";

export interface CategorySlice {
  readonly category: string;
  readonly count: number;
}

/** Fatias com itens; a chave `unclassified` vira "Sem categoria". */
export function categorySlices(
  byCategory: Readonly<Record<string, number>>,
  categoryName: (categoryId: number) => string | undefined,
): CategorySlice[] {
  return Object.entries(byCategory)
    .filter(([, count]) => count > 0)
    .map(([key, count]) => {
      if (key === UNCLASSIFIED_KEY) {
        return { category: "Sem categoria", count };
      }
      const id = Number(key);
      return { category: categoryName(id) ?? `Categoria #${key}`, count };
    });
}

export function QuarantinePieChart({
  byCategory,
  categoryName,
}: Readonly<{
  byCategory: Readonly<Record<string, number>> | null;
  categoryName: (categoryId: number) => string | undefined;
}>): ReactElement {
  const data = byCategory === null ? [] : categorySlices(byCategory, categoryName);
  return (
    <ChartFrame label="Quarentena por categoria" empty={data.length === 0}>
      <Pie data={data} angleField="count" colorField="category" height={240} />
    </ChartFrame>
  );
}
