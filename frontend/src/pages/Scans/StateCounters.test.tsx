// SPDX-License-Identifier: GPL-3.0-only
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { metricsSummary } from "../../test/metrics-fixtures";

import { StateCounters } from "./StateCounters";

describe("StateCounters", () => {
  it("mostra itens por estado e o backlog por etapa", () => {
    render(<StateCounters summary={metricsSummary()} />);

    expect(screen.getByText("Descoberto")).toBeInTheDocument();
    expect(screen.getByText("Sugerido")).toBeInTheDocument();
    expect(screen.getByText("Aguardando extração")).toBeInTheDocument();
    expect(screen.getByText("Aguardando classificação")).toBeInTheDocument();
    expect(screen.getByText("Aguardando revisão")).toBeInTheDocument();
    expect(screen.getAllByText("40")).toHaveLength(2);
  });
});
