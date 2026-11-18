// SPDX-License-Identifier: GPL-3.0-only
import { Result, Typography } from "antd";
import type { ReactElement } from "react";
import { Navigate, Route, Routes } from "react-router-dom";

import { AppLayout } from "./AppLayout";
import { MENU_ITEMS } from "./menu";

const HOME_PATH = "/revisao";

function Placeholder({ title }: Readonly<{ title: string }>): ReactElement {
  return (
    <Result
      status="info"
      title={<Typography.Title level={2}>{title}</Typography.Title>}
      subTitle="Em construção"
    />
  );
}

export function AppRoutes(): ReactElement {
  return (
    <Routes>
      <Route path="/login" element={<Placeholder title="Entrar na conta Microsoft" />} />
      <Route element={<AppLayout />}>
        <Route index element={<Navigate to={HOME_PATH} replace />} />
        {MENU_ITEMS.map((item) => (
          <Route key={item.key} path={item.key} element={<Placeholder title={item.label} />} />
        ))}
        <Route path="*" element={<Result status="404" title="Página não encontrada" />} />
      </Route>
    </Routes>
  );
}
