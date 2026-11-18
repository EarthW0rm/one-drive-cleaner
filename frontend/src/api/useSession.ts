// SPDX-License-Identifier: GPL-3.0-only
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { useEffect } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { ApiError, getSession, type SessionResponse } from "./client";

const SESSION_KEY = ["session"] as const;
const LOGIN_PATH = "/login";

/** Sessão atual; `LOGIN_REQUIRED` (erro ou `auth_state`) navega para `/login`. */
export function useSession(): UseQueryResult<SessionResponse> {
  const query = useQuery({ queryKey: SESSION_KEY, queryFn: getSession });
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const loginRequired =
    query.data?.auth_state === "login_required" ||
    (query.error instanceof ApiError && query.error.code === "LOGIN_REQUIRED");

  useEffect(() => {
    if (loginRequired && pathname !== LOGIN_PATH) {
      void navigate(LOGIN_PATH, { replace: true });
    }
  }, [loginRequired, pathname, navigate]);

  return query;
}
