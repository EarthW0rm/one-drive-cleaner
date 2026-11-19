// SPDX-License-Identifier: GPL-3.0-only
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ModelsResponse } from "../../api/models";
import { CATEGORIES, models } from "../../test/review-fixtures";

import { ReviewProgress, type ReviewProgressProps } from "./ReviewProgress";

function renderProgress(overrides: Partial<ReviewProgressProps> = {}): void {
  render(
    <ReviewProgress
      models={models()}
      loading={false}
      failed={false}
      retrainEvery={20}
      categoryName={(id) => CATEGORIES.find((category) => category.id === id)?.name}
      {...overrides}
    />,
  );
}

function withGate(overrides: Partial<ModelsResponse["gate"]>): ModelsResponse {
  const base = models();
  return { ...base, gate: { ...base.gate, ...overrides } };
}

describe("ReviewProgress", () => {
  it("mostra o progresso até o próximo retreino", () => {
    renderProgress();

    const bar = screen.getByRole("progressbar", { name: "Progresso até o próximo retreino" });
    expect(bar).toHaveAttribute("aria-valuenow", "75");
    expect(screen.getByText("15 / 20")).toBeInTheDocument();
  });

  it("limita o progresso a 100% quando o limite foi ultrapassado", () => {
    renderProgress({ models: withGate({ labels: 400 }) });

    expect(
      screen.getByRole("progressbar", { name: "Progresso até o próximo retreino" }),
    ).toHaveAttribute("aria-valuenow", "100");
    expect(screen.getByText("20 / 20")).toBeInTheDocument();
  });

  it("mostra a distância até a meta do modo Automático", () => {
    renderProgress();

    expect(screen.getByText("Meta ainda não atingida")).toBeInTheDocument();
    expect(screen.getByText("/ 100")).toBeInTheDocument();
    expect(screen.getByText("/ 0,98")).toBeInTheDocument();
    expect(screen.getByText("0,93")).toBeInTheDocument();
    expect(screen.getByText("/ 30")).toBeInTheDocument();
  });

  it("indica meta atingida e precisão ainda não medida", () => {
    renderProgress({ models: withGate({ eligible: true, precision: null }) });

    expect(screen.getByText("Meta atingida")).toBeInTheDocument();
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("lista a precisão por categoria do modelo vigente", () => {
    renderProgress();

    expect(screen.getByText(/Foto real: 97% \(30\)/)).toBeInTheDocument();
    expect(screen.getByText(/Print de tela: 92% \(12\)/)).toBeInTheDocument();
  });

  it("usa o id da categoria sem nome conhecido e omite a seção sem métricas", () => {
    const base = models();
    const current =
      base.current === null
        ? null
        : { ...base.current, metrics: { per_category: { "9": { precision: 0.5, support: 1 } } } };
    renderProgress({ models: { ...base, current } });

    expect(screen.getByText(/Categoria #9: 50% \(1\)/)).toBeInTheDocument();
  });

  it("não mostra precisão por categoria quando não há modelo vigente", () => {
    renderProgress({ models: { ...models(), current: null } });

    expect(screen.queryByText("Precisão por categoria")).not.toBeInTheDocument();
  });

  it("mostra esqueleto enquanto carrega", () => {
    renderProgress({ models: undefined, loading: true });

    expect(screen.getByLabelText("Carregando estado do modelo")).toBeInTheDocument();
  });

  it.each([
    ["erro", { failed: true }],
    ["ausência de dados", { models: undefined }],
  ])("avisa quando o estado do modelo não está disponível (%s)", (_name, props) => {
    renderProgress(props);

    expect(screen.getByText("Não foi possível carregar o estado do modelo.")).toBeInTheDocument();
  });
});
