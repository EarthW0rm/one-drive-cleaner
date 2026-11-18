// SPDX-License-Identifier: GPL-3.0-only
import { ApiError, type ApiErrorCode } from "../api/client";

export const ERROR_MESSAGES: Record<ApiErrorCode, string> = {
  VALIDATION_ERROR: "Os dados informados são inválidos.",
  PROTECTED_CATEGORY: "Esta categoria é protegida e não pode ser alterada.",
  NOT_IN_QUARANTINE: "O item não está na quarentena.",
  INVALID_CONFIRMATION: "A confirmação informada não confere.",
  LOGIN_REQUIRED: "É necessário entrar na conta Microsoft.",
  CONFLICT: "A operação conflita com o estado atual. Atualize e tente novamente.",
  NOT_FOUND: "O recurso solicitado não foi encontrado.",
  INTERNAL: "Ocorreu um erro interno. Tente novamente.",
  NETWORK_ERROR:
    "Não foi possível falar com a aplicação local. Verifique se o odc está em execução.",
};

/** Mensagem PT-BR para qualquer erro capturado na interface. */
export function errorMessage(error: unknown): string {
  return error instanceof ApiError ? ERROR_MESSAGES[error.code] : ERROR_MESSAGES.INTERNAL;
}
