// SPDX-License-Identifier: GPL-3.0-only
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { ApiError, getSession, type SessionResponse } from "./client";

export const SESSION_KEY = ["session"] as const;
export const LOGIN_PATH = "/login";
/** Valor de `location.state.reason` quando a sessão expira e o utilizador vai para `/login`. */
export const SESSION_EXPIRED_REASON = "session_expired";

export type SessionState = UseQueryResult<SessionResponse> & {
  /** Sessão que já esteve autenticada nesta montagem e passou a `login_required`. */
  readonly sessionExpired: boolean;
};

/**
 * Sessão atual. `LOGIN_REQUIRED` (erro ou `auth_state`) sem sessão autenticada prévia navega
 * para `/login`; uma sessão que expira depois de autenticada mantém a rota e sinaliza
 * `sessionExpired` para o banner de reconexão.
 */
export function useSession(): SessionState {
  const query = useQuery({ queryKey: SESSION_KEY, queryFn: getSession });
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const [wasAuthenticated, setWasAuthenticated] = useState(false);
  if (query.data?.auth_state === "authenticated" && !wasAuthenticated) {
    setWasAuthenticated(true);
  }
  const loginRequired =
    query.data?.auth_state === "login_required" ||
    (query.error instanceof ApiError && query.error.code === "LOGIN_REQUIRED");
  const redirectToLogin = loginRequired && !wasAuthenticated;

  useEffect(() => {
    if (redirectToLogin && pathname !== LOGIN_PATH) {
      void navigate(LOGIN_PATH, { replace: true });
    }
  }, [redirectToLogin, pathname, navigate]);

  return { ...query, sessionExpired: wasAuthenticated && loginRequired };
}
