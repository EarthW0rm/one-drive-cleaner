# SPDX-License-Identifier: GPL-3.0-only
"""Serviço de autenticação MSAL (auth code + PKCE, conta pessoal) com cache no Keychain.

Fluxo: `begin_login` gera a URL de autorização (state/nonce/PKCE do MSAL) e guarda o fluxo em
memória por `state` (TTL 10 min, uso único); `complete_login` troca o código, exige
`driveType=personal` e só então persiste o cache no Keychain sob o `account_key`. Tokens, e-mail e
`id_token` nunca vão para banco, log ou telemetria; o único dado de perfil guardado é o nome
exibível, no registro da conta ativa (Keychain).

O serviço também satisfaz o `AuthGateway` da camada web (`auth_state`, `display_name`) sem
rede e sem levantar exceção.
"""

import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, Protocol
from urllib.parse import urlsplit

from msal import SerializableTokenCache
from pydantic import ValidationError

from odc.account_key import account_key
from odc.auth.drive_check import check_personal_drive
from odc.auth.errors import (
    AccountSwitchError,
    AuthFailedError,
    AuthStateError,
    ConfigurationError,
    KeychainUnavailableError,
    LoginRequired,
    TokenRefreshError,
)
from odc.auth.models import (
    Account,
    AccountRecord,
    AuthFlow,
    AuthState,
    sanitize_display_name,
)
from odc.auth.msal_client import (
    AccountInfo,
    Flow,
    MsalClient,
    MsalFactory,
    TokenResult,
    default_app_factory,
)
from odc.auth.token_cache import (
    ACTIVE_ACCOUNT_NAME,
    EMPTY_CONTENT,
    KeychainTokenStore,
    TokenStore,
    build_token_cache,
    read_entry,
    write_entry,
)
from odc.telemetry.metrics import LoginReason, RefreshResult

logger = logging.getLogger(__name__)

AUTHORITY: Final = "https://login.microsoftonline.com/consumers"
# `offline_access` é reservado no MSAL e acrescentado por ele (refresh token); o contrato da
# TF-146-01 (`Files.ReadWrite offline_access`) fica satisfeito na URL de autorização.
SCOPES: Final = ("Files.ReadWrite",)
CALLBACK_PATH: Final = "/auth/callback"
LOOPBACK_HOSTS: Final = frozenset({"localhost", "127.0.0.1"})
FLOW_TTL_S: Final = 600.0
MAX_PENDING_FLOWS: Final = 16
SELECT_ACCOUNT_PROMPT: Final = "select_account"
# Só estes parâmetros do callback chegam ao MSAL; o restante é ignorado.
RESPONSE_KEYS: Final = frozenset({"code", "state", "error", "client_info", "session_state"})
# Erros do provedor que significam refresh token inválido: o utilizador precisa entrar de novo.
REVOKED_ERRORS: Final = frozenset(
    {"invalid_grant", "interaction_required", "consent_required", "login_required"}
)
_SAFE_CODE = re.compile(r"^[a-z_]{1,40}$")
DEFAULT_ERROR_CODE: Final = "auth_failed"


class DriveChecker(Protocol):
    """Valida que o drive é pessoal e devolve seu `id` (`UnsupportedAccount` se não for)."""

    def __call__(self, access_token: str) -> str: ...


class AuthMetrics(Protocol):
    """Subconjunto de `odc.telemetry.metrics.Metrics` usado pela autenticação."""

    def record_token_refresh(self, *, result: RefreshResult) -> None: ...

    def record_login_required(self, *, reason: LoginReason) -> None: ...


@dataclass(slots=True)
class _PendingFlow:
    flow: Flow
    expires_at: float


def _safe_code(value: object) -> str:
    """Código de erro OAuth em formato fechado; qualquer outra coisa vira o padrão."""
    if isinstance(value, str) and _SAFE_CODE.fullmatch(value):
        return value
    return DEFAULT_ERROR_CODE


def _validate_redirect_uri(redirect_uri: str) -> None:
    try:
        parts = urlsplit(redirect_uri)
        parts.port  # noqa: B018 - valida a porta (ValueError se inválida)
    except ValueError as error:
        message = "redirect_uri inválido"
        raise ValueError(message) from error
    valid = (
        parts.scheme == "http"
        and parts.hostname in LOOPBACK_HOSTS
        and parts.path == CALLBACK_PATH
        and not (parts.username or parts.password or parts.query or parts.fragment)
    )
    if not valid:
        message = f"redirect_uri deve ser http://localhost[:porta]{CALLBACK_PATH}"
        raise ValueError(message)


class AuthService:
    """Obtém e renova tokens da conta pessoal; uma conta ativa por vez (RN-01)."""

    def __init__(  # noqa: PLR0913 - dependências injetáveis (Keychain, MSAL, Graph) para testes
        self,
        client_id: str | None,
        *,
        store: TokenStore | None = None,
        metrics: AuthMetrics | None = None,
        drive_checker: DriveChecker = check_personal_drive,
        app_factory: MsalFactory = default_app_factory,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client_id = client_id
        self._store: TokenStore = store if store is not None else KeychainTokenStore()
        self._metrics = metrics
        self._drive_checker = drive_checker
        self._app_factory = app_factory
        self._clock = clock
        self._lock = threading.RLock()  # estado em memória (curto, sem I/O de rede)
        self._login_lock = threading.Lock()  # serializa complete_login
        self._refresh_lock = threading.Lock()  # serializa a renovação de token
        self._flows: dict[str, _PendingFlow] = {}
        self._record: AccountRecord | None = None
        self._loaded = False
        self._app: MsalClient | None = None
        self._login_required: LoginReason | None = None

    # --- Login ---

    def begin_login(self, redirect_uri: str) -> AuthFlow:
        """Inicia o fluxo e devolve a URL de autorização (PKCE, `state` e `nonce` no MSAL)."""
        client_id = self._require_client_id()
        _validate_redirect_uri(redirect_uri)
        app = self._app_factory(client_id, AUTHORITY, SerializableTokenCache())
        try:
            flow = app.initiate_auth_code_flow(
                list(SCOPES), redirect_uri=redirect_uri, prompt=SELECT_ACCOUNT_PROMPT
            )
        except (OSError, ValueError) as error:
            message = "não foi possível iniciar o login"
            raise AuthFailedError(message) from error
        auth_uri, state = flow.get("auth_uri"), flow.get("state")
        if not (isinstance(auth_uri, str) and isinstance(state, str) and auth_uri and state):
            message = "o MSAL não gerou o fluxo de autorização"
            raise AuthFailedError(message)
        with self._lock:
            self._prune_flows()
            self._flows[state] = _PendingFlow(flow, self._clock() + FLOW_TTL_S)
        return AuthFlow(auth_uri=auth_uri, state=state)

    def complete_login(self, flow_state: str, query_params: Mapping[str, str]) -> Account:
        """Conclui o login com os parâmetros do callback e devolve a conta ativa.

        Levanta `AuthStateError` (state inválido), `AuthFailedError` (recusa/falha),
        `UnsupportedAccount` (conta não pessoal; nada é persistido), `AccountSwitchError`
        (há outra conta ativa) ou `KeychainUnavailableError`.
        """
        with self._login_lock:
            pending = self._take_flow(flow_state)
            client_id = self._require_client_id()
            memory_cache = SerializableTokenCache()
            app = self._app_factory(client_id, AUTHORITY, memory_cache)
            result = self._redeem(app, pending.flow, flow_state, query_params)
            access_token = self._access_token(result)
            home_account_id = self._home_account_id(app)
            key = account_key(home_account_id)
            self._ensure_same_account(key)
            drive_id = self._drive_checker(access_token)
            record = AccountRecord(
                account_key=key,
                display_name=sanitize_display_name(self._claim(result, "name")),
                drive_id=drive_id,
                home_account_id=home_account_id,
            )
            self._activate(record, memory_cache, client_id)
            logger.info("login concluído")
            return record.to_account()

    # --- Tokens ---

    def get_access_token(self, *, force_refresh: bool = False) -> str:
        """Token de acesso silencioso. Levanta `LoginRequired` quando exige interação.

        `force_refresh` ignora o access token em cache e renova no provedor (usado pelo cliente
        da Graph após um 401). `TokenRefreshError` indica falha transitória (rede/serviço): não
        exige novo login.
        """
        record, app = self._active_session()
        account = self._find_account(app, record)
        with self._refresh_lock:
            try:
                result = app.acquire_token_silent_with_error(
                    list(SCOPES), account, force_refresh=force_refresh
                )
            except OSError as error:
                self._record_refresh(RefreshResult.FAILED)
                message = "falha de rede ao renovar o token"
                raise TokenRefreshError(message) from error
        return self._token_from(result)

    def logout(self) -> None:
        """Remove o cache de tokens da conta (Keychain) e a conta ativa; idempotente."""
        with self._lock:
            record = self._load_record()
            if record is not None:
                self._store.delete(record.account_key)
            self._store.delete(ACTIVE_ACCOUNT_NAME)
            self._record = None
            self._loaded = True
            self._app = None
            self._flows.clear()
            self._login_required = None
        logger.info("logout concluído")

    def active_account(self) -> Account | None:
        """Conta ativa (mesmo com login exigido, para manter o banco da conta); sem rede."""
        record = self._load_record()
        return record.to_account() if record is not None else None

    # --- AuthGateway (camada web) ---

    def auth_state(self) -> AuthState:
        """`authenticated` se há conta ativa sem login pendente; nunca levanta exceção."""
        try:
            record = self._load_record()
        except KeychainUnavailableError:
            logger.error("Keychain indisponível ao consultar a conta ativa")
            return "login_required"
        with self._lock:
            ready = record is not None and self._login_required is None
        return "authenticated" if ready else "login_required"

    def display_name(self) -> str | None:
        """Nome exibível da conta autenticada; `None` sem conta ou com login exigido."""
        if self.auth_state() != "authenticated":
            return None
        record = self._load_record()
        return record.display_name if record is not None else None

    # --- Internos: configuração e fluxos ---

    def _require_client_id(self) -> str:
        if not self._client_id:
            message = (
                "client_id ausente: defina ODC_CLIENT_ID (ou client_id no config.toml) "
                "com o ID do aplicativo registrado no Microsoft Entra."
            )
            raise ConfigurationError(message)
        return self._client_id

    def _prune_flows(self) -> None:
        now = self._clock()
        for state in [key for key, item in self._flows.items() if item.expires_at <= now]:
            del self._flows[state]
        while len(self._flows) >= MAX_PENDING_FLOWS:
            del self._flows[next(iter(self._flows))]

    def _take_flow(self, state: str) -> _PendingFlow:
        with self._lock:
            pending = self._flows.pop(state, None)
        if pending is None or pending.expires_at <= self._clock():
            message = "state desconhecido ou expirado"
            raise AuthStateError(message)
        return pending

    # --- Internos: conclusão do login ---

    @staticmethod
    def _redeem(
        app: MsalClient, flow: Flow, flow_state: str, query_params: Mapping[str, str]
    ) -> TokenResult:
        response = {
            key: value
            for key, value in query_params.items()
            if key in RESPONSE_KEYS and isinstance(value, str)
        }
        if response.get("state") != flow_state:
            message = "state do callback não confere"
            raise AuthStateError(message)
        try:
            result = app.acquire_token_by_auth_code_flow(flow, response)
        except ValueError as error:
            message = "callback inválido para o fluxo iniciado"
            raise AuthStateError(message) from error
        except OSError as error:
            message = "falha de rede na troca do código de autorização"
            raise AuthFailedError(message) from error
        if "error" in result:
            code = _safe_code(result.get("error"))
            logger.warning("login recusado pelo provedor", extra={"error_code": code})
            message = f"login recusado pelo provedor ({code})"
            raise AuthFailedError(message, code=code)
        return result

    @staticmethod
    def _access_token(result: TokenResult) -> str:
        token = result.get("access_token")
        if not isinstance(token, str) or not token:
            message = "o provedor não devolveu token de acesso"
            raise AuthFailedError(message)
        return token

    @staticmethod
    def _claim(result: TokenResult, name: str) -> object:
        claims = result.get("id_token_claims")
        return claims.get(name) if isinstance(claims, Mapping) else None

    @staticmethod
    def _home_account_id(app: MsalClient) -> str:
        for account in app.get_accounts():
            home_account_id = account.get("home_account_id")
            if isinstance(home_account_id, str) and home_account_id:
                return home_account_id
        message = "o provedor não devolveu a conta autenticada"
        raise AuthFailedError(message)

    def _ensure_same_account(self, key: str) -> None:
        current = self._load_record()
        if current is not None and current.account_key != key:
            message = "há outra conta ativa: faça logout antes de entrar com outra conta"
            raise AccountSwitchError(message)

    def _activate(
        self, record: AccountRecord, memory_cache: SerializableTokenCache, client_id: str
    ) -> None:
        persistence = self._store.persistence(record.account_key)
        cache = build_token_cache(persistence)
        try:
            cache.deserialize(memory_cache.serialize())
            write_entry(persistence, cache.serialize())
            write_entry(self._store.persistence(ACTIVE_ACCOUNT_NAME), record.model_dump_json())
        except KeychainUnavailableError:
            self._discard_best_effort(record.account_key)
            raise
        app = self._app_factory(client_id, AUTHORITY, cache)
        with self._lock:
            self._record = record
            self._loaded = True
            self._app = app
            self._login_required = None

    def _discard_best_effort(self, key: str) -> None:
        try:
            self._store.delete(key)
        except KeychainUnavailableError:
            logger.error("não foi possível limpar o cache parcial no Keychain")

    # --- Internos: conta ativa e token silencioso ---

    def _load_record(self) -> AccountRecord | None:
        with self._lock:
            if not self._loaded:
                self._record = self._read_record()
                self._loaded = True
            return self._record

    def _read_record(self) -> AccountRecord | None:
        raw = read_entry(self._store.persistence(ACTIVE_ACCOUNT_NAME))
        if raw is None or raw == EMPTY_CONTENT:
            return None
        try:
            return AccountRecord.model_validate_json(raw)
        except ValidationError:
            logger.warning("registro da conta ativa inválido; tratado como ausente")
            return None

    def _active_session(self) -> tuple[AccountRecord, MsalClient]:
        with self._lock:
            record = self._load_record()
            if record is None:
                raise self._need_login(LoginReason.MISSING)
            if self._app is None:
                client_id = self._require_client_id()
                cache = build_token_cache(self._store.persistence(record.account_key))
                self._app = self._app_factory(client_id, AUTHORITY, cache)
            return record, self._app

    def _find_account(self, app: MsalClient, record: AccountRecord) -> AccountInfo:
        for account in app.get_accounts():
            if account.get("home_account_id") == record.home_account_id:
                return account
        raise self._need_login(LoginReason.MISSING)

    def _token_from(self, result: TokenResult | None) -> str:
        if result is None:
            raise self._need_login(LoginReason.EXPIRED)
        error = result.get("error")
        if error is not None:
            self._record_refresh(RefreshResult.FAILED)
            code = _safe_code(error)
            if error in REVOKED_ERRORS:
                raise self._need_login(LoginReason.REVOKED)
            message = f"renovação de token falhou ({code})"
            raise TokenRefreshError(message)
        token = result.get("access_token")
        if not isinstance(token, str) or not token:
            self._record_refresh(RefreshResult.FAILED)
            message = "o provedor não devolveu token de acesso"
            raise TokenRefreshError(message)
        if result.get("token_source") == "identity_provider":
            self._record_refresh(RefreshResult.OK)
        with self._lock:
            self._login_required = None
        return token

    def _need_login(self, reason: LoginReason) -> LoginRequired:
        """Marca o login como exigido (métrica só na transição) e devolve a exceção."""
        with self._lock:
            first = self._login_required is None
            self._login_required = reason
        if first and self._metrics is not None:
            self._metrics.record_login_required(reason=reason)
        return LoginRequired(reason)

    def _record_refresh(self, result: RefreshResult) -> None:
        if self._metrics is not None:
            self._metrics.record_token_refresh(result=result)
