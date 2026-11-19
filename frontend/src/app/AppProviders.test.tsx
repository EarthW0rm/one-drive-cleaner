// SPDX-License-Identifier: GPL-3.0-only
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Pagination, theme } from "antd";
import { type ReactElement } from "react";
import { describe, expect, it } from "vitest";

import { AppProviders, buildTheme } from "./AppProviders";
import { THEME_STORAGE_KEY, useTheme } from "./theme";

function Probe(): ReactElement {
  const { mode, toggle } = useTheme();
  const { token } = theme.useToken();
  return (
    <button type="button" onClick={toggle}>
      {mode}:{token.colorBgBase}
    </button>
  );
}

const MIN_CONTRAST = 4.5;

type Rgb = readonly [number, number, number];

/** Cor `#rgb`, `#rrggbb` ou `rgb[a](...)` composta sobre o fundo `over` (RGB 0-255). */
function toRgb(color: string, over: Rgb = [255, 255, 255]): Rgb {
  const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(color)?.[1];
  if (hex !== undefined) {
    const full = hex.length === 3 ? hex.replaceAll(/./g, "$&$&") : hex;
    return [0, 2, 4].map((at) => Number.parseInt(full.slice(at, at + 2), 16)) as unknown as Rgb;
  }
  const parts = /^rgba?\(([^)]+)\)$/.exec(color)?.[1]?.split(",").map(Number) ?? [];
  const [red = 0, green = 0, blue = 0, alpha = 1] = parts;
  return [red, green, blue].map(
    (channel, at) => channel * alpha + (over[at] ?? 0) * (1 - alpha),
  ) as unknown as Rgb;
}

function luminance([red, green, blue]: Rgb): number {
  const [r = 0, g = 0, b = 0] = [red, green, blue].map((channel) => {
    const unit = channel / 255;
    return unit <= 0.03928 ? unit / 12.92 : ((unit + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(foreground: string, background: string): number {
  const back = toRgb(background);
  const [lighter = 0, darker = 0] = [luminance(toRgb(foreground, back)), luminance(back)].sort(
    (a, b) => b - a,
  );
  return (lighter + 0.05) / (darker + 0.05);
}

describe.each(["light", "dark"] as const)("buildTheme (contraste WCAG AA no tema %s)", (mode) => {
  const token = theme.getDesignToken(buildTheme(mode));

  it.each([
    ["texto principal", token.colorText, token.colorBgContainer],
    [
      "texto de apoio (Statistic, Steps, secundário)",
      token.colorTextDescription,
      token.colorBgContainer,
    ],
    ["placeholder", token.colorTextPlaceholder, token.colorBgContainer],
    ["texto sobre a cor primária (botão, menu)", token.colorTextLightSolid, token.colorPrimary],
    ["link", token.colorLink, token.colorBgContainer],
    ["link em hover", token.colorLinkHover, token.colorBgContainer],
    ["link ativo", token.colorLinkActive, token.colorBgContainer],
    ["texto sobre a cor primária em hover", token.colorTextLightSolid, token.colorPrimaryHover],
    ["texto sobre a cor primária ativa", token.colorTextLightSolid, token.colorPrimaryActive],
    ["texto de sucesso (Tag)", token.colorSuccess, token.colorSuccessBg],
    ["texto de sucesso (colorSuccessText)", token.colorSuccessText, token.colorSuccessBg],
    ["texto de erro (botão perigoso)", token.colorError, token.colorBgContainer],
    ["texto de aviso", token.colorWarning, token.colorBgContainer],
  ])("%s atinge %s:1", (_name, foreground, background) => {
    expect(contrast(foreground, background)).toBeGreaterThanOrEqual(MIN_CONTRAST);
  });
});

describe("AppProviders", () => {
  it("aplica o locale pt-BR do antd", () => {
    render(
      <AppProviders>
        <Pagination total={100} showSizeChanger />
      </AppProviders>,
    );

    expect(screen.getAllByText(/\/ página/).length).toBeGreaterThan(0);
  });

  it("alterna o algoritmo do tema e persiste a escolha", async () => {
    const user = userEvent.setup();
    render(
      <AppProviders>
        <Probe />
      </AppProviders>,
    );
    const button = screen.getByRole("button");
    expect(button).toHaveTextContent(/^light:#fff/);

    await user.click(button);

    expect(button).toHaveTextContent(/^dark:#000/);
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");

    await user.click(button);

    expect(button).toHaveTextContent(/^light:/);
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("light");
  });

  it("restaura a preferência salva", () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, "dark");

    render(
      <AppProviders>
        <Probe />
      </AppProviders>,
    );

    expect(screen.getByRole("button")).toHaveTextContent(/^dark:/);
  });
});
