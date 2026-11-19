// SPDX-License-Identifier: GPL-3.0-only
import { Result } from "antd";
import type { ReactElement } from "react";
import { Navigate, Route, Routes } from "react-router-dom";

import { CategoriesPage } from "../pages/Categories/CategoriesPage";
import { LoginPage } from "../pages/Login/LoginPage";
import { MetricsPage } from "../pages/Metrics/MetricsPage";
import { OnboardingPage } from "../pages/Onboarding/OnboardingPage";
import { QuarantinePage } from "../pages/Quarantine/QuarantinePage";
import { ReviewPage } from "../pages/Review/ReviewPage";
import { ScansPage } from "../pages/Scans/ScansPage";
import { SettingsPage } from "../pages/Settings/SettingsPage";

import { AppLayout, ONBOARDING_PATH } from "./AppLayout";

const HOME_PATH = "/revisao";
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
        <Route path="/metricas" element={<MetricsPage />} />
        <Route path="/configuracoes" element={<SettingsPage />} />
        <Route path="*" element={<Result status="404" title="Página não encontrada" />} />
      </Route>
    </Routes>
  );
}
