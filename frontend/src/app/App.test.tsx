// SPDX-License-Identifier: GPL-3.0-only
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { App } from "./App";

describe("App", () => {
  it("renderiza o título da página placeholder", () => {
    render(<App />);

    expect(screen.getByRole("heading", { name: "OneDrive Cleaner" })).toBeInTheDocument();
  });

  it("aplica o locale pt_BR do antd", () => {
    render(<App />);

    expect(screen.getAllByText("Não há dados").length).toBeGreaterThan(0);
  });
});
