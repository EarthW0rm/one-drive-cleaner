// SPDX-License-Identifier: GPL-3.0-only
import { Result, Typography } from "antd";
import type { ReactElement } from "react";
import { Navigate, Route, Routes } from "react-router-dom";

import { CategoriesPage } from "../pages/Categories/CategoriesPage";
import { LoginPage } from "../pages/Login/LoginPage";
import { OnboardingPage } from "../pages/Onboarding/OnboardingPage";
import { QuarantinePage } from "../pages/Quarantine/QuarantinePage";
import { ReviewPage } from "../pages/Review/ReviewPage";
import { ScansPage } from "../pages/Scans/ScansPage";
import { SettingsPage } from "../pages/Settings/SettingsPage";

import { AppLayout, ONBOARDING_PATH } from "./AppLayout";
import { MENU_ITEMS } from "./menu";

const HOME_PATH = "/revisao";
const ROUTED_KEYS = new Set<string>([
  "/revisao",
  "/quarentena",
  "/categorias",
  "/varreduras",
  "/configuracoes",
]);

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
      <Route path="/login" element={<LoginPage />} />
      <Route element={<AppLayout />}>
        <Route index element={<Navigate to={HOME_PATH} replace />} />
        <Route path={ONBOARDING_PATH} element={<OnboardingPage />} />
        <Route path="/revisao" element={<ReviewPage />} />
        <Route path="/quarentena" element={<QuarantinePage />} />
        <Route path="/categorias" element={<CategoriesPage />} />
        <Route path="/varreduras" element={<ScansPage />} />
        <Route path="/configuracoes" element={<SettingsPage />} />
        {MENU_ITEMS.filter((item) => !ROUTED_KEYS.has(item.key)).map((item) => (
          <Route key={item.key} path={item.key} element={<Placeholder title={item.label} />} />
        ))}
        <Route path="*" element={<Result status="404" title="Página não encontrada" />} />
      </Route>
    </Routes>
  );
}
