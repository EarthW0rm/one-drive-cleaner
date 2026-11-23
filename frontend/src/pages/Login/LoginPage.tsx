// SPDX-License-Identifier: GPL-3.0-only
import { Alert, Button, Result, Typography } from "antd";
import type { ReactElement } from "react";
import { useLocation, useSearchParams } from "react-router-dom";

import { SESSION_EXPIRED_REASON } from "../../api/useSession";

const GENERIC_ERROR = "Não foi possível concluir a entrada. Tente novamente.";

/** Lista fechada de códigos de `/login?error=` (ver `odc.web.routes.auth`). */
const LOGIN_ERRORS = new Map<string, string>([
  ["auth_failed", GENERIC_ERROR],
  ["unsupported_account", "Somente contas pessoais da Microsoft são aceitas."],
  ["not_configured", "A aplicação não está configurada. Defina ODC_CLIENT_ID e reinicie o odc."],
]);

const EXPIRED_MESSAGE = "Sua sessão expirou. Entre novamente para continuar.";

function locationReason(state: unknown): string | null {
  if (typeof state === "object" && state !== null && "reason" in state) {
    return typeof state.reason === "string" ? state.reason : null;
  }
  return null;
}

/** Mensagem para o erro informado na URL; o valor bruto nunca é renderizado. */
function errorAlertMessage(code: string | null): string | null {
  if (code === null) {
    return null;
  }
  return LOGIN_ERRORS.get(code) ?? GENERIC_ERROR;
}

export function LoginPage(): ReactElement {
  const [params] = useSearchParams();
  const location = useLocation();
  const errorMessage = errorAlertMessage(params.get("error"));
  const expired = locationReason(location.state as unknown) === SESSION_EXPIRED_REASON;

  return (
    <Result
      status="info"
      title={<Typography.Title level={2}>Entrar na conta Microsoft</Typography.Title>}
      subTitle="Somente contas pessoais. A senha não passa pela aplicação; os tokens ficam no Keychain do macOS."
      extra={
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          {expired ? <Alert type="warning" showIcon title={EXPIRED_MESSAGE} /> : null}
          {errorMessage === null ? null : <Alert type="error" showIcon title={errorMessage} />}
          <Button type="primary" href="/auth/login">
            Entrar com conta Microsoft
          </Button>
        </div>
      }
    />
  );
}
