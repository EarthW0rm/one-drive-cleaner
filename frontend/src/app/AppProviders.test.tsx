// SPDX-License-Identifier: GPL-3.0-only
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Pagination, theme } from "antd";
import { type ReactElement } from "react";
import { describe, expect, it } from "vitest";

import { AppProviders } from "./AppProviders";
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
