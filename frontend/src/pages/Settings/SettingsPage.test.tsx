// SPDX-License-Identifier: GPL-3.0-only
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { createQueryClient } from "../../api/queryClient";
import { errorResponse, sessionHandler, TEST_ORIGIN } from "../../test/handlers";
import { server } from "../../test/server";

import { buildPatch, SettingsPage, type SettingsFormValues } from "./SettingsPage";

const MIB = 1024 * 1024;
const URL_SETTINGS = `${TEST_ORIGIN}/api/settings`;
const URL_LOGOUT = `${TEST_ORIGIN}/auth/logout`;

interface SettingsFixture {
  automation_mode: "manual" | "auto" | "evolutive";
  auto_threshold: number;
  retrain_every: number;
  temp_cap_bytes: number;
  otel_enabled: boolean;
  otel_endpoint: string | null;
  restart_required: boolean;
}

function settings(overrides: Partial<SettingsFixture> = {}): SettingsFixture {
  return {
    automation_mode: "evolutive",
    auto_threshold: 0.98,
    retrain_every: 20,
    temp_cap_bytes: 2048 * MIB,
    otel_enabled: true,
    otel_endpoint: "http://127.0.0.1:4317",
    restart_required: false,
    ...overrides,
  };
}

/** Registra os corpos enviados em PUT e responde com o estado atual mesclado ao enviado. */
function settingsHandlers(current: SettingsFixture = settings()) {
  const bodies: unknown[] = [];
  const handlers = [
    sessionHandler(),
    http.get(URL_SETTINGS, () => HttpResponse.json(current)),
    http.put(URL_SETTINGS, async ({ request }) => {
      const body: unknown = await request.json();
      bodies.push(body);
      return HttpResponse.json({ ...current, ...(body as object), restart_required: false });
    }),
  ];
  return { bodies, handlers };
}

function renderPage(): void {
  render(
    <QueryClientProvider client={createQueryClient(0)}>
      <MemoryRouter initialEntries={["/configuracoes"]}>
        <SettingsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function openForm(): Promise<void> {
  await screen.findByRole("button", { name: "Salvar" });
}

function saveButton(): HTMLElement {
  return screen.getByRole("button", { name: "Salvar" });
}

describe("SettingsPage", () => {
  it("mostra os valores atuais, com teto de temporário em MB", async () => {
    const { handlers } = settingsHandlers();
    server.use(...handlers);

    renderPage();
    await openForm();

    expect(screen.getByText("Configurações")).toBeInTheDocument();
    expect(
      screen.getByRole("spinbutton", { name: "Frequência de retreino (valor numérico)" }),
    ).toHaveValue("20");
    expect(
      screen.getByRole("spinbutton", { name: "Limiar de confiança (valor numérico)" }),
    ).toHaveValue("0.980");
    expect(screen.getByLabelText("Teto de espaço temporário (MB)")).toHaveValue("2048");
    expect(screen.getByRole("switch", { name: "Telemetria local" })).toBeChecked();
    expect(screen.getByLabelText("Endereço do coletor de telemetria")).toHaveValue(
      "http://127.0.0.1:4317",
    );
  });

  it("envia só o campo alterado e confirma o salvamento", async () => {
    const user = userEvent.setup();
    const { bodies, handlers } = settingsHandlers();
    server.use(...handlers);

    renderPage();
    await openForm();
    await user.click(screen.getByText("Automático"));
    await user.click(saveButton());

    expect(await screen.findByText("Configurações salvas.")).toBeInTheDocument();
    expect(bodies).toEqual([{ automation_mode: "auto" }]);
  });

  it("não envia nada quando não há alteração", async () => {
    const user = userEvent.setup();
    const { bodies, handlers } = settingsHandlers();
    server.use(...handlers);

    renderPage();
    await openForm();
    await user.click(saveButton());

    expect(await screen.findByText("Nenhuma alteração para salvar.")).toBeInTheDocument();
    expect(bodies).toEqual([]);
  });

  it("aplica o limiar digitado no campo numérico", async () => {
    const user = userEvent.setup();
    const { bodies, handlers } = settingsHandlers();
    server.use(...handlers);

    renderPage();
    await openForm();
    const threshold = screen.getByRole("spinbutton", {
      name: "Limiar de confiança (valor numérico)",
    });
    await user.clear(threshold);
    await user.type(threshold, "0.9");
    await user.click(saveButton());

    await screen.findByText("Configurações salvas.");
    expect(bodies).toEqual([{ auto_threshold: 0.9 }]);
  });

  it("bloqueia endpoint de telemetria fora de loopback sem chamar a API", async () => {
    const user = userEvent.setup();
    const { bodies, handlers } = settingsHandlers();
    server.use(...handlers);

    renderPage();
    await openForm();
    const endpoint = screen.getByLabelText("Endereço do coletor de telemetria");
    await user.clear(endpoint);
    // eslint-disable-next-line sonarjs/no-clear-text-protocols -- endpoint fora de loopback (teste de bloqueio)
    await user.type(endpoint, "http://10.0.0.5:4317");
    await user.click(saveButton());

    expect(
      await screen.findByText("Use um endereço de loopback: 127.0.0.1, localhost ou ::1."),
    ).toBeInTheDocument();
    expect(bodies).toEqual([]);
  });

  it("aceita endpoint de loopback em IPv6 e localhost", async () => {
    const user = userEvent.setup();
    const { bodies, handlers } = settingsHandlers();
    server.use(...handlers);

    renderPage();
    await openForm();
    const endpoint = screen.getByLabelText("Endereço do coletor de telemetria");
    await user.clear(endpoint);
    // eslint-disable-next-line sonarjs/no-clear-text-protocols -- loopback IPv6 em http (teste de aceite)
    await user.type(endpoint, "http://[[::1]:4317");
    await user.click(saveButton());

    await screen.findByText("Configurações salvas.");
    expect(bodies).toEqual([{ otel_endpoint: "http://[::1]:4317" }]);
  });

  it("não permite limpar um endpoint já salvo", async () => {
    const user = userEvent.setup();
    const { bodies, handlers } = settingsHandlers();
    server.use(...handlers);

    renderPage();
    await openForm();
    await user.clear(screen.getByLabelText("Endereço do coletor de telemetria"));
    await user.click(saveButton());

    expect(
      await screen.findByText("Use um endereço de loopback: 127.0.0.1, localhost ou ::1."),
    ).toBeInTheDocument();
    expect(bodies).toEqual([]);
  });

  it("limita a frequência de retreino à faixa 5 a 500 ao sair do campo", async () => {
    const user = userEvent.setup();
    const { bodies, handlers } = settingsHandlers();
    server.use(...handlers);

    renderPage();
    await openForm();
    const retrain = screen.getByRole("spinbutton", {
      name: "Frequência de retreino (valor numérico)",
    });
    await user.clear(retrain);
    await user.type(retrain, "1");
    await user.click(saveButton());

    await screen.findByText("Configurações salvas.");
    expect(bodies).toEqual([{ retrain_every: 5 }]);
  });

  it("exige valor no limiar", async () => {
    const user = userEvent.setup();
    const { bodies, handlers } = settingsHandlers();
    server.use(...handlers);

    renderPage();
    await openForm();
    const threshold = screen.getByRole("spinbutton", {
      name: "Limiar de confiança (valor numérico)",
    });
    await user.clear(threshold);
    await user.click(saveButton());

    expect(await screen.findByText("Informe o limiar.")).toBeInTheDocument();
    expect(bodies).toEqual([]);
  });

  it("limita o teto de temporário ao mínimo de 256 MB", async () => {
    const user = userEvent.setup();
    const { bodies, handlers } = settingsHandlers();
    server.use(...handlers);

    renderPage();
    await openForm();
    const cap = screen.getByLabelText("Teto de espaço temporário (MB)");
    await user.clear(cap);
    await user.type(cap, "100");
    await user.click(saveButton());

    await screen.findByText("Configurações salvas.");
    expect(bodies).toEqual([{ temp_cap_bytes: 256 * MIB }]);
  });

  it("exige valor no teto de temporário", async () => {
    const user = userEvent.setup();
    const { bodies, handlers } = settingsHandlers();
    server.use(...handlers);

    renderPage();
    await openForm();
    await user.clear(screen.getByLabelText("Teto de espaço temporário (MB)"));
    await user.click(saveButton());

    expect(await screen.findByText("Informe o teto de espaço temporário.")).toBeInTheDocument();
    expect(bodies).toEqual([]);
  });

  it("converte o teto de MB para bytes ao salvar", async () => {
    const user = userEvent.setup();
    const { bodies, handlers } = settingsHandlers();
    server.use(...handlers);

    renderPage();
    await openForm();
    const cap = screen.getByLabelText("Teto de espaço temporário (MB)");
    await user.clear(cap);
    await user.type(cap, "512");
    await user.click(saveButton());

    await screen.findByText("Configurações salvas.");
    expect(bodies).toEqual([{ temp_cap_bytes: 512 * MIB }]);
  });

  it("mostra o aviso de reinício quando a telemetria muda", async () => {
    const user = userEvent.setup();
    server.use(
      sessionHandler(),
      http.get(URL_SETTINGS, () => HttpResponse.json(settings())),
      http.put(URL_SETTINGS, () =>
        HttpResponse.json(settings({ otel_enabled: false, restart_required: true })),
      ),
    );

    renderPage();
    await openForm();
    await user.click(screen.getByRole("switch", { name: "Telemetria local" }));
    await user.click(saveButton());

    expect(
      await screen.findByText("Alterações de telemetria só valem após reiniciar a aplicação."),
    ).toBeInTheDocument();
  });

  it("marca o campo quando a API rejeita o valor (422)", async () => {
    const user = userEvent.setup();
    server.use(
      sessionHandler(),
      http.get(URL_SETTINGS, () => HttpResponse.json(settings())),
      http.put(URL_SETTINGS, () =>
        HttpResponse.json(
          {
            error: {
              code: "VALIDATION_ERROR",
              message: "Requisição inválida.",
              details: {
                fields: [{ loc: ["body", "temp_cap_bytes"], type: "greater_than_equal" }],
              },
            },
          },
          { status: 422 },
        ),
      ),
    );

    renderPage();
    await openForm();
    await user.click(screen.getByText("Automático"));
    await user.click(saveButton());

    expect(await screen.findByText("Valor inválido para este campo.")).toBeInTheDocument();
    expect(screen.getByText("Os dados informados são inválidos.")).toBeInTheDocument();
  });

  it("mostra erro geral quando o salvamento falha fora do 422", async () => {
    const user = userEvent.setup();
    server.use(
      sessionHandler(),
      http.get(URL_SETTINGS, () => HttpResponse.json(settings())),
      http.put(URL_SETTINGS, () => errorResponse("INTERNAL", "falha", 500)),
    );

    renderPage();
    await openForm();
    await user.click(screen.getByText("Automático"));
    await user.click(saveButton());

    expect(
      await screen.findByText("Ocorreu um erro interno. Tente novamente."),
    ).toBeInTheDocument();
  });

  it("avisa quando a rota de configurações não existe (404)", async () => {
    server.use(http.get(URL_SETTINGS, () => errorResponse("NOT_FOUND", "x", 404)));

    renderPage();

    expect(await screen.findByText("Configurações indisponíveis")).toBeInTheDocument();
  });

  it("mostra erro de carregamento com a mensagem da API", async () => {
    server.use(http.get(URL_SETTINGS, () => errorResponse("INTERNAL", "x", 500)));

    renderPage();

    expect(await screen.findByText("Falha ao carregar as configurações")).toBeInTheDocument();
    expect(screen.getByText("Ocorreu um erro interno. Tente novamente.")).toBeInTheDocument();
  });

  it("encerra a conta pelo botão Sair da conta", async () => {
    const user = userEvent.setup();
    let logoutCalls = 0;
    const { handlers } = settingsHandlers();
    server.use(
      ...handlers,
      http.post(URL_LOGOUT, () => {
        logoutCalls += 1;
        return new HttpResponse(null, { status: 204 });
      }),
    );

    renderPage();
    await openForm();
    await user.click(screen.getByRole("button", { name: "Sair da conta" }));

    await waitFor(() => {
      expect(logoutCalls).toBe(1);
    });
  });

  it("mostra erro quando o logout falha", async () => {
    const user = userEvent.setup();
    const { handlers } = settingsHandlers();
    server.use(
      ...handlers,
      http.post(URL_LOGOUT, () => errorResponse("INTERNAL", "x", 500)),
    );

    renderPage();
    await openForm();
    await user.click(screen.getByRole("button", { name: "Sair da conta" }));

    expect(
      await screen.findByText("Ocorreu um erro interno. Tente novamente."),
    ).toBeInTheDocument();
  });
});

describe("buildPatch", () => {
  const saved = settings({ otel_endpoint: null });
  const values: SettingsFormValues = {
    automation_mode: "evolutive",
    auto_threshold: 0.98,
    retrain_every: 20,
    temp_cap_mb: 2048,
    otel_enabled: true,
    otel_endpoint: "",
  };

  it("devolve vazio quando nada mudou e o endpoint continua nulo", () => {
    expect(buildPatch(values, saved)).toEqual({});
  });

  it("ignora campos numéricos vazios (a validação do formulário os barra antes)", () => {
    expect(
      buildPatch(
        { ...values, auto_threshold: null, retrain_every: null, temp_cap_mb: null },
        saved,
      ),
    ).toEqual({});
  });

  it("inclui o endpoint apenas quando preenchido e diferente do salvo", () => {
    expect(buildPatch({ ...values, otel_endpoint: "http://localhost:4317" }, saved)).toEqual({
      otel_endpoint: "http://localhost:4317",
    });
  });
});
