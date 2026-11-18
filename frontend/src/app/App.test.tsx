// SPDX-License-Identifier: GPL-3.0-only
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { sessionHandler } from "../test/handlers";
import { server } from "../test/server";

import { App } from "./App";

describe("App", () => {
  it("renderiza o casco e a tela inicial de Revisão", async () => {
    server.use(sessionHandler());

    render(<App />);

    expect(await screen.findByText("Maria Teste")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Revisão" })).toBeInTheDocument();
    expect(screen.getByText("OneDrive Cleaner")).toBeInTheDocument();
  });
});
