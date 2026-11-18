// SPDX-License-Identifier: GPL-3.0-only
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { App as AntApp, ConfigProvider, theme } from "antd";
import ptBR from "antd/locale/pt_BR";
import { useMemo, useState, type ReactElement, type ReactNode } from "react";
import { BrowserRouter } from "react-router-dom";

import { createQueryClient } from "../api/queryClient";

import { persistTheme, readInitialTheme, ThemeContext, type ThemeMode } from "./theme";

const defaultQueryClient = createQueryClient();

interface AppProvidersProps {
  readonly children: ReactNode;
  readonly queryClient?: QueryClient;
}

export function AppProviders({
  children,
  queryClient = defaultQueryClient,
}: AppProvidersProps): ReactElement {
  const [mode, setMode] = useState<ThemeMode>(readInitialTheme);
  const value = useMemo(
    () => ({
      mode,
      toggle: () => {
        const next: ThemeMode = mode === "dark" ? "light" : "dark";
        persistTheme(next);
        setMode(next);
      },
    }),
    [mode],
  );

  return (
    <ThemeContext value={value}>
      <ConfigProvider
        locale={ptBR}
        theme={{ algorithm: mode === "dark" ? theme.darkAlgorithm : theme.defaultAlgorithm }}
      >
        <AntApp>
          <QueryClientProvider client={queryClient}>
            <BrowserRouter>{children}</BrowserRouter>
          </QueryClientProvider>
        </AntApp>
      </ConfigProvider>
    </ThemeContext>
  );
}
