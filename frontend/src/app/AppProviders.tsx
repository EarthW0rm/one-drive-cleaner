// SPDX-License-Identifier: GPL-3.0-only
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { App as AntApp, ConfigProvider, theme, type ThemeConfig } from "antd";
import ptBR from "antd/locale/pt_BR";
import { useMemo, useState, type ReactElement, type ReactNode } from "react";
import { BrowserRouter } from "react-router-dom";

import { createQueryClient } from "../api/queryClient";

import { persistTheme, readInitialTheme, ThemeContext, type ThemeMode } from "./theme";

const defaultQueryClient = createQueryClient();

/**
 * Tokens que levam o tema padrão do antd ao contraste mínimo WCAG AA (4,5:1 em texto normal):
 * cor primária mais escura (texto branco sobre ela e links), textos de apoio, placeholder,
 * sucesso, erro e aviso mais fortes e cabeçalho com texto claro (a barra escura não pode herdar
 * o texto escuro do conteúdo). O tema escuro parte do algoritmo do antd e corrige só os textos de
 * apoio, o placeholder e o erro.
 */
export function buildTheme(mode: ThemeMode): ThemeConfig {
  const dark = mode === "dark";
  return {
    algorithm: dark ? theme.darkAlgorithm : theme.defaultAlgorithm,
    token: {
      colorPrimary: "#0958d9",
      // O estado de hover também precisa de contraste (texto branco no botão, link sobre branco).
      colorPrimaryHover: "#1a6adb",
      colorPrimaryActive: "#003eb3",
      ...(dark
        ? {
            // Sobre o fundo escuro o link precisa ser claro; o preenchimento primário segue escuro.
            colorLink: "#69b1ff",
            colorLinkHover: "#91caff",
            colorLinkActive: "#4096ff",
            colorTextDescription: "#a6a6a6",
            colorTextPlaceholder: "#8c8c8c",
            colorError: "#ff7875",
          }
        : {
            colorLink: "#0958d9",
            colorLinkHover: "#1a6adb",
            colorLinkActive: "#003eb3",
            // O Tag de sucesso usa `colorSuccess` sobre `colorSuccessBg`: fixar o fundo claro evita
            // que o antd o derive escuro a partir da cor mais forte.
            colorSuccess: "#237804",
            colorSuccessText: "#237804",
            colorSuccessBg: "#f6ffed",
            colorSuccessBorder: "#b7eb8f",
            colorError: "#cf1322",
            colorWarning: "#874d00",
            colorTextDescription: "#595959",
            colorTextPlaceholder: "#6b6b6b",
          }),
    },
    components: { Layout: { headerColor: "#ffffff" } },
  };
}

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
      <ConfigProvider locale={ptBR} theme={buildTheme(mode)}>
        <AntApp>
          <QueryClientProvider client={queryClient}>
            <BrowserRouter>{children}</BrowserRouter>
          </QueryClientProvider>
        </AntApp>
      </ConfigProvider>
    </ThemeContext>
  );
}
