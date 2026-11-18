// SPDX-License-Identifier: GPL-3.0-only
import { render, screen } from "@testing-library/react";
import { MemoryRouter, type InitialEntry } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { LoginPage } from "./LoginPage";

function renderAt(entry: InitialEntry): void {
  render(
    <MemoryRouter initialEntries={[entry]}>
      <LoginPage />
    </MemoryRouter>,
  );
}

describe("LoginPage", () => {
  it("oferece o botão de entrada apontando para /auth/login", () => {
    renderAt("/login");

    const button = screen.getByRole("link", { name: "Entrar com conta Microsoft" });
    expect(button).toHaveAttribute("href", "/auth/login");
  });

  it("explica a privacidade do acesso", () => {
    renderAt("/login");

    expect(screen.getByText(/Somente contas pessoais/)).toBeInTheDocument();
  });

  it.each([
    ["auth_failed", "Não foi possível concluir a entrada. Tente novamente."],
    ["unsupported_account", "Somente contas pessoais da Microsoft são aceitas."],
    ["not_configured", "A aplicação não está configurada. Defina ODC_CLIENT_ID e reinicie o odc."],
  ])("exibe a mensagem do erro %s", (code, message) => {
    renderAt(`/login?error=${code}`);

    expect(screen.getByText(message)).toBeInTheDocument();
  });

  it("exibe mensagem genérica para código desconhecido sem renderizar o valor bruto", () => {
    renderAt("/login?error=<img src=x>");

    expect(
      screen.getByText("Não foi possível concluir a entrada. Tente novamente."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/<img/)).not.toBeInTheDocument();
  });

  it("não exibe alerta sem parâmetro de erro", () => {
    renderAt("/login");

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("informa a expiração quando a navegação vem da sessão expirada", () => {
    renderAt({ pathname: "/login", state: { reason: "session_expired" } });

    expect(
      screen.getByText("Sua sessão expirou. Entre novamente para continuar."),
    ).toBeInTheDocument();
  });

  it("ignora state de motivo desconhecido", () => {
    renderAt({ pathname: "/login", state: { reason: "outra" } });

    expect(screen.queryByText(/Sua sessão expirou/)).not.toBeInTheDocument();
  });

  it("ignora state sem motivo textual", () => {
    renderAt({ pathname: "/login", state: { reason: 42 } });

    expect(screen.queryByText(/Sua sessão expirou/)).not.toBeInTheDocument();
  });
});
