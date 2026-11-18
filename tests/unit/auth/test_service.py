# SPDX-License-Identifier: GPL-3.0-only
import io
import logging
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from odc.account_key import account_key
from odc.auth.errors import (
    AccountSwitchError,
    AuthFailedError,
    AuthStateError,
    ConfigurationError,
    KeychainUnavailableError,
    LoginRequired,
    TokenRefreshError,
    UnsupportedAccount,
)
from odc.auth.models import DEFAULT_DISPLAY_NAME, Account
from odc.auth.service import (
    AUTHORITY,
    FLOW_TTL_S,
    MAX_PENDING_FLOWS,
    AuthService,
)
from odc.auth.token_cache import ACTIVE_ACCOUNT_NAME
from odc.telemetry.logging import JsonFormatter, RedactionFilter
from odc.telemetry.metrics import LoginReason, Metrics, RefreshResult

from .fakes import (
    ACCESS_TOKEN,
    CLIENT_ID,
    DISPLAY_NAME,
    DRIVE_ID,
    EMAIL,
    HOME_ACCOUNT_ID,
    REDIRECT_URI,
    REFRESH_TOKEN,
    STATE,
    MemoryTokenStore,
    MsalScript,
    SpyMetrics,
)

KEY = account_key(HOME_ACCOUNT_ID)
CALLBACK = {"code": "FAKE-auth-code", "state": STATE}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Harness:
    """Serviço sob teste com Keychain, MSAL e Graph falsos."""

    def __init__(self, tmp_path: Path, client_id: str | None = CLIENT_ID) -> None:
        self.script = MsalScript()
        self.store = MemoryTokenStore(tmp_path)
        self.metrics = SpyMetrics()
        self.clock = Clock()
        self.drive_tokens: list[str] = []
        self.drive_error: Exception | None = None
        self.client_id = client_id
        self.service = self.new_service()

    def _drive(self, access_token: str) -> str:
        self.drive_tokens.append(access_token)
        if self.drive_error is not None:
            raise self.drive_error
        return DRIVE_ID

    def new_service(self) -> AuthService:
        return AuthService(
            self.client_id,
            store=self.store,
            metrics=self.metrics,
            drive_checker=self._drive,
            app_factory=self.script.factory,
            clock=self.clock,
        )

    def login(self) -> Account:
        self.service.begin_login(REDIRECT_URI)
        return self.service.complete_login(STATE, CALLBACK)


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


# --- begin_login ---


def test_begin_login_devolve_url_e_state_e_guarda_o_fluxo(h: Harness) -> None:
    flow = h.service.begin_login(REDIRECT_URI)

    assert flow.auth_uri == h.script.flow["auth_uri"]
    assert flow.state == STATE
    assert h.script.created == [(CLIENT_ID, AUTHORITY)]
    scopes, redirect, prompt = h.script.initiated[0]
    assert scopes == ["Files.ReadWrite"]
    assert redirect == REDIRECT_URI
    assert prompt == "select_account"


def test_authority_e_consumers() -> None:
    assert AUTHORITY == "https://login.microsoftonline.com/consumers"


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "https://localhost:8765/auth/callback",
        "http://evil.example/auth/callback",
        "http://localhost:8765/outro",
        "http://localhost:8765/auth/callback?next=http://evil.example",
        "http://localhost:8765/auth/callback#frag",
        "http://user:senha@localhost:8765/auth/callback",
        "http://localhost:99999999/auth/callback",
        "",
    ],
)
def test_begin_login_recusa_redirect_uri_fora_do_loopback(h: Harness, redirect_uri: str) -> None:
    with pytest.raises(ValueError, match="redirect_uri"):
        h.service.begin_login(redirect_uri)


@pytest.mark.parametrize("client_id", [None, ""])
def test_begin_login_sem_client_id_orienta_configuracao(tmp_path: Path, client_id: str) -> None:
    service = Harness(tmp_path, client_id=client_id).service

    with pytest.raises(ConfigurationError, match="ODC_CLIENT_ID"):
        service.begin_login(REDIRECT_URI)


def test_begin_login_converte_falha_do_msal(h: Harness) -> None:
    h.script.initiate_error = OSError("sem rede")

    with pytest.raises(AuthFailedError):
        h.service.begin_login(REDIRECT_URI)


@pytest.mark.parametrize("flow", [{}, {"auth_uri": "https://x"}, {"state": "s"}, {"auth_uri": 1}])
def test_begin_login_recusa_fluxo_incompleto(h: Harness, flow: dict[str, object]) -> None:
    h.script.flow = flow

    with pytest.raises(AuthFailedError):
        h.service.begin_login(REDIRECT_URI)


def test_fluxo_expira_apos_o_ttl(h: Harness) -> None:
    h.service.begin_login(REDIRECT_URI)
    h.clock.now += FLOW_TTL_S + 1

    with pytest.raises(AuthStateError):
        h.service.complete_login(STATE, CALLBACK)


def test_fluxo_vale_ate_o_ttl(h: Harness) -> None:
    h.service.begin_login(REDIRECT_URI)
    h.clock.now += FLOW_TTL_S - 1

    assert h.service.complete_login(STATE, CALLBACK).account_key == KEY


def test_fluxos_pendentes_sao_limitados_descartando_o_mais_antigo(h: Harness) -> None:
    for index in range(MAX_PENDING_FLOWS + 1):
        h.script.flow = {"auth_uri": "https://login.example/a", "state": f"s{index}"}
        h.service.begin_login(REDIRECT_URI)

    with pytest.raises(AuthStateError):
        h.service.complete_login("s0", {"code": "c", "state": "s0"})
    h.script.flow = {"auth_uri": "https://login.example/a", "state": STATE}
    assert (
        h.service.complete_login(f"s{MAX_PENDING_FLOWS}", {"state": f"s{MAX_PENDING_FLOWS}"})
        is not None
    )


def test_fluxos_expirados_sao_descartados_ao_iniciar_outro(h: Harness) -> None:
    h.service.begin_login(REDIRECT_URI)
    h.clock.now += FLOW_TTL_S + 1
    h.script.flow = {"auth_uri": "https://login.example/a", "state": "novo"}
    h.service.begin_login(REDIRECT_URI)

    with pytest.raises(AuthStateError):
        h.service.complete_login(STATE, CALLBACK)


# --- complete_login: sucesso ---


def test_login_conclui_persiste_cache_e_registro_no_keychain(h: Harness) -> None:
    account = h.login()

    assert account == Account(account_key=KEY, display_name=DISPLAY_NAME, drive_id=DRIVE_ID)
    assert h.drive_tokens == [ACCESS_TOKEN]
    assert REFRESH_TOKEN in h.store.entries[KEY]
    record = h.store.entries[ACTIVE_ACCOUNT_NAME]
    for secret in (ACCESS_TOKEN, REFRESH_TOKEN, EMAIL, "id_token"):
        assert secret not in record
    assert set(h.store.entries) == {KEY, ACTIVE_ACCOUNT_NAME}
    assert h.service.active_account() == account
    assert h.service.auth_state() == "authenticated"
    assert h.service.display_name() == DISPLAY_NAME


def test_login_envia_ao_msal_so_parametros_conhecidos(h: Harness) -> None:
    h.service.begin_login(REDIRECT_URI)

    h.service.complete_login(
        STATE, {**CALLBACK, "client_info": "abc", "redirect": "http://evil.example", "x": "1"}
    )

    assert h.script.redeemed == [{"code": "FAKE-auth-code", "state": STATE, "client_info": "abc"}]


def test_nome_ausente_usa_nome_padrao_e_nunca_o_email(h: Harness) -> None:
    h.script.redeem_result = {"access_token": ACCESS_TOKEN}

    account = h.login()

    assert account.display_name == DEFAULT_DISPLAY_NAME


def test_login_repetido_da_mesma_conta_e_aceito(h: Harness) -> None:
    h.login()

    assert h.login().account_key == KEY


# --- complete_login: falhas ---


def test_state_desconhecido_levanta_auth_state_error(h: Harness) -> None:
    with pytest.raises(AuthStateError):
        h.service.complete_login("inexistente", {"state": "inexistente", "code": "c"})


def test_state_do_callback_diferente_do_fluxo_e_recusado(h: Harness) -> None:
    h.service.begin_login(REDIRECT_URI)

    with pytest.raises(AuthStateError):
        h.service.complete_login(STATE, {"state": "outro", "code": "c"})

    assert h.script.redeemed == []


def test_state_so_pode_ser_usado_uma_vez(h: Harness) -> None:
    h.login()

    with pytest.raises(AuthStateError):
        h.service.complete_login(STATE, CALLBACK)


def test_valueerror_do_msal_vira_auth_state_error(h: Harness) -> None:
    h.service.begin_login(REDIRECT_URI)
    h.script.redeem_error = ValueError("state mismatch")

    with pytest.raises(AuthStateError):
        h.service.complete_login(STATE, CALLBACK)


def test_falha_de_rede_na_troca_do_codigo(h: Harness) -> None:
    h.service.begin_login(REDIRECT_URI)
    h.script.redeem_error = OSError("sem rede")

    with pytest.raises(AuthFailedError):
        h.service.complete_login(STATE, CALLBACK)


@pytest.mark.parametrize(
    ("provider_error", "expected"),
    [
        ("access_denied", "access_denied"),
        ("<script>alert(1)</script>", "auth_failed"),
        (7, "auth_failed"),
    ],
)
def test_erro_do_provedor_nao_reflete_texto_livre(
    h: Harness, provider_error: object, expected: str
) -> None:
    h.service.begin_login(REDIRECT_URI)
    h.script.redeem_result = {
        "error": provider_error,
        "error_description": f"AADSTS: {EMAIL} {ACCESS_TOKEN}",
    }

    with pytest.raises(AuthFailedError) as caught:
        h.service.complete_login(STATE, CALLBACK)

    assert caught.value.code == expected
    assert EMAIL not in str(caught.value)
    assert h.store.entries == {}


@pytest.mark.parametrize("result", [{}, {"access_token": ""}, {"access_token": 5}])
def test_resposta_sem_token_de_acesso_e_falha(h: Harness, result: dict[str, object]) -> None:
    h.service.begin_login(REDIRECT_URI)
    h.script.redeem_result = result

    with pytest.raises(AuthFailedError):
        h.service.complete_login(STATE, CALLBACK)


@pytest.mark.parametrize("accounts", [[], [{"username": EMAIL}], [{"home_account_id": ""}]])
def test_resposta_sem_conta_e_falha(h: Harness, accounts: list[Mapping[str, object]]) -> None:
    h.service.begin_login(REDIRECT_URI)
    h.script.accounts = accounts

    with pytest.raises(AuthFailedError):
        h.service.complete_login(STATE, CALLBACK)

    assert h.store.entries == {}


def test_conta_nao_pessoal_e_recusada_sem_persistir_cache(h: Harness) -> None:
    h.service.begin_login(REDIRECT_URI)
    h.drive_error = UnsupportedAccount("business")

    with pytest.raises(UnsupportedAccount):
        h.service.complete_login(STATE, CALLBACK)

    assert h.store.entries == {}
    assert h.service.active_account() is None
    assert h.service.auth_state() == "login_required"


def test_falha_do_drive_check_nao_persiste(h: Harness) -> None:
    h.service.begin_login(REDIRECT_URI)
    h.drive_error = AuthFailedError("rede")

    with pytest.raises(AuthFailedError):
        h.service.complete_login(STATE, CALLBACK)

    assert h.store.entries == {}


def test_outra_conta_exige_logout_antes(h: Harness) -> None:
    h.login()
    h.script.accounts = [{"home_account_id": "outro.id"}]
    h.service.begin_login(REDIRECT_URI)

    with pytest.raises(AccountSwitchError):
        h.service.complete_login(STATE, CALLBACK)

    assert h.service.active_account() is not None
    assert account_key("outro.id") not in h.store.entries

    h.service.logout()
    h.service.begin_login(REDIRECT_URI)
    other = h.service.complete_login(STATE, CALLBACK)
    assert other.account_key == account_key("outro.id")
    assert h.drive_tokens.count(ACCESS_TOKEN) == 2


def test_keychain_inacessivel_ao_gravar_cache_nao_deixa_resto(h: Harness) -> None:
    h.service.begin_login(REDIRECT_URI)
    h.store.failing.add(KEY)

    with pytest.raises(KeychainUnavailableError):
        h.service.complete_login(STATE, CALLBACK)

    assert h.service.active_account() is None
    assert ACTIVE_ACCOUNT_NAME not in h.store.entries


def test_keychain_inacessivel_ao_gravar_registro_remove_o_cache(h: Harness) -> None:
    h.service.begin_login(REDIRECT_URI)
    h.store.failing.add(ACTIVE_ACCOUNT_NAME)

    with pytest.raises(KeychainUnavailableError):
        h.service.complete_login(STATE, CALLBACK)

    assert KEY not in h.store.entries
    assert KEY in h.store.deleted


def test_limpeza_parcial_com_keychain_travado_nao_mascara_o_erro(h: Harness) -> None:
    h.service.begin_login(REDIRECT_URI)
    h.store.failing.update({KEY, ACTIVE_ACCOUNT_NAME})

    with pytest.raises(KeychainUnavailableError):
        h.service.complete_login(STATE, CALLBACK)


# --- get_access_token ---


def test_token_sem_conta_exige_login_missing(h: Harness) -> None:
    with pytest.raises(LoginRequired) as caught:
        h.service.get_access_token()

    assert caught.value.reason == "missing"
    assert h.metrics.logins == [LoginReason.MISSING]
    assert h.service.auth_state() == "login_required"


def test_login_exigido_conta_uma_vez_por_episodio(h: Harness) -> None:
    for _ in range(3):
        with pytest.raises(LoginRequired):
            h.service.get_access_token()

    assert h.metrics.logins == [LoginReason.MISSING]


def test_token_do_cache_nao_conta_refresh(h: Harness) -> None:
    h.login()

    assert h.service.get_access_token() == ACCESS_TOKEN
    assert h.metrics.refreshes == []
    assert h.script.silent_calls == 1


def test_token_renovado_no_provedor_conta_refresh_ok(h: Harness) -> None:
    h.login()
    h.script.silent_result = {"access_token": "FAKE-novo", "token_source": "identity_provider"}

    assert h.service.get_access_token() == "FAKE-novo"
    assert h.metrics.refreshes == [RefreshResult.OK]


def test_refresh_revogado_exige_login_e_mantem_a_conta(h: Harness) -> None:
    h.login()
    h.script.silent_result = {"error": "invalid_grant", "error_description": EMAIL}

    with pytest.raises(LoginRequired) as caught:
        h.service.get_access_token()

    assert caught.value.reason == "revoked"
    assert h.metrics.refreshes == [RefreshResult.FAILED]
    assert h.metrics.logins == [LoginReason.REVOKED]
    assert h.service.auth_state() == "login_required"
    assert h.service.display_name() is None
    assert h.service.active_account() is not None
    assert KEY in h.store.entries


def test_login_novo_apos_revogacao_restaura_a_sessao(h: Harness) -> None:
    h.login()
    h.script.silent_result = {"error": "invalid_grant"}
    with pytest.raises(LoginRequired):
        h.service.get_access_token()

    h.login()

    assert h.service.auth_state() == "authenticated"


def test_sem_token_no_cache_exige_login_expired(h: Harness) -> None:
    h.login()
    h.script.silent_result = None

    with pytest.raises(LoginRequired) as caught:
        h.service.get_access_token()

    assert caught.value.reason == "expired"


def test_conta_ausente_do_cache_msal_exige_login_missing(h: Harness) -> None:
    h.login()
    h.script.accounts = []

    with pytest.raises(LoginRequired) as caught:
        h.service.get_access_token()

    assert caught.value.reason == "missing"


def test_erro_transitorio_nao_exige_login(h: Harness) -> None:
    h.login()
    h.script.silent_result = {"error": "temporarily_unavailable"}

    with pytest.raises(TokenRefreshError, match="temporarily_unavailable"):
        h.service.get_access_token()

    assert h.metrics.refreshes == [RefreshResult.FAILED]
    assert h.metrics.logins == []
    assert h.service.auth_state() == "authenticated"


def test_falha_de_rede_na_renovacao_nao_exige_login(h: Harness) -> None:
    h.login()
    h.script.silent_error = OSError("sem rede")

    with pytest.raises(TokenRefreshError):
        h.service.get_access_token()

    assert h.metrics.refreshes == [RefreshResult.FAILED]
    assert h.service.auth_state() == "authenticated"


@pytest.mark.parametrize("result", [{}, {"access_token": ""}])
def test_resposta_silenciosa_sem_token_e_falha_transitoria(
    h: Harness, result: dict[str, object]
) -> None:
    h.login()
    h.script.silent_result = result

    with pytest.raises(TokenRefreshError):
        h.service.get_access_token()


def test_token_obtido_limpa_o_login_exigido(h: Harness) -> None:
    h.login()
    h.script.silent_result = None
    with pytest.raises(LoginRequired):
        h.service.get_access_token()
    h.script.silent_result = {"access_token": ACCESS_TOKEN, "token_source": "cache"}

    assert h.service.get_access_token() == ACCESS_TOKEN
    assert h.service.auth_state() == "authenticated"


def test_sem_metricas_o_servico_funciona(tmp_path: Path) -> None:
    script = MsalScript()
    service = AuthService(
        CLIENT_ID,
        store=MemoryTokenStore(tmp_path),
        drive_checker=lambda _t: DRIVE_ID,
        app_factory=script.factory,
    )
    with pytest.raises(LoginRequired):
        service.get_access_token()
    service.begin_login(REDIRECT_URI)
    service.complete_login(STATE, CALLBACK)
    script.silent_result = {"error": "invalid_grant"}

    with pytest.raises(LoginRequired):
        service.get_access_token()


def test_metricas_reais_do_catalogo(tmp_path: Path) -> None:
    reader = InMemoryMetricReader()
    metrics = Metrics.create(MeterProvider(metric_readers=[reader]))
    script = MsalScript()
    service = AuthService(
        CLIENT_ID,
        store=MemoryTokenStore(tmp_path),
        metrics=metrics,
        drive_checker=lambda _t: DRIVE_ID,
        app_factory=script.factory,
    )

    with pytest.raises(LoginRequired):
        service.get_access_token()

    data = reader.get_metrics_data()
    assert data is not None
    names = {
        metric.name
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }
    assert "odc.auth.login_required" in names


# --- Reinício do processo ---


def test_conta_persiste_entre_instancias_e_renova_pelo_cache_do_keychain(h: Harness) -> None:
    account = h.login()
    restarted = h.new_service()

    assert restarted.active_account() == account
    assert restarted.auth_state() == "authenticated"
    assert restarted.get_access_token() == ACCESS_TOKEN
    cache = h.script.apps[-1].cache
    assert cache.search("RefreshToken")


def test_instancia_nova_sem_client_id_nao_renova_mas_expoe_a_conta(
    h: Harness, tmp_path: Path
) -> None:
    h.login()
    h.client_id = None
    restarted = h.new_service()

    assert restarted.active_account() is not None
    with pytest.raises(ConfigurationError):
        restarted.get_access_token()


def test_sessao_sem_registro_e_login_missing_apos_reinicio(h: Harness) -> None:
    restarted = h.new_service()

    assert restarted.active_account() is None
    assert restarted.display_name() is None


def test_registro_corrompido_e_tratado_como_ausente(
    h: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    h.store.entries[ACTIVE_ACCOUNT_NAME] = '{"account_key": "nao-e-hash"}'

    with caplog.at_level(logging.WARNING):
        assert h.service.active_account() is None

    assert "inválido" in caplog.text


def test_registro_limpo_pelo_logout_nao_gera_aviso(
    h: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    h.store.entries[ACTIVE_ACCOUNT_NAME] = "{}"

    with caplog.at_level(logging.WARNING):
        assert h.service.active_account() is None

    assert caplog.text == ""


def test_auth_state_nao_levanta_com_keychain_inacessivel(h: Harness) -> None:
    h.store.unreadable.add(ACTIVE_ACCOUNT_NAME)

    assert h.service.auth_state() == "login_required"
    assert h.service.display_name() is None
    with pytest.raises(KeychainUnavailableError):
        h.service.active_account()


# --- logout ---


def test_logout_remove_cache_e_conta(h: Harness) -> None:
    h.login()

    h.service.logout()

    assert h.store.entries == {}
    assert set(h.store.deleted) == {KEY, ACTIVE_ACCOUNT_NAME}
    assert h.service.active_account() is None
    assert h.service.auth_state() == "login_required"
    with pytest.raises(LoginRequired):
        h.service.get_access_token()


def test_logout_e_idempotente(h: Harness) -> None:
    h.service.logout()
    h.service.logout()

    assert h.store.deleted == [ACTIVE_ACCOUNT_NAME, ACTIVE_ACCOUNT_NAME]


def test_logout_invalida_fluxos_pendentes(h: Harness) -> None:
    h.service.begin_login(REDIRECT_URI)

    h.service.logout()

    with pytest.raises(AuthStateError):
        h.service.complete_login(STATE, CALLBACK)


def test_logout_com_keychain_travado_falha_e_preserva_a_conta(h: Harness) -> None:
    h.login()
    h.store.failing.add(KEY)

    with pytest.raises(KeychainUnavailableError):
        h.service.logout()

    assert h.service.active_account() is not None


# --- Logs sem segredos (RN-24; filtro da TF-145-02 ativo) ---


@pytest.fixture
def filtered_logs(caplog: pytest.LogCaptureFixture) -> Iterator[io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactionFilter())
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    caplog.set_level(logging.DEBUG)
    yield stream
    root.removeHandler(handler)


def test_nenhum_log_contem_token_email_ou_codigo(
    h: Harness, filtered_logs: io.StringIO, caplog: pytest.LogCaptureFixture
) -> None:
    h.login()
    h.script.silent_result = {
        "error": "invalid_grant",
        "error_description": f"{EMAIL} {ACCESS_TOKEN}",
    }
    with pytest.raises(LoginRequired):
        h.service.get_access_token()
    h.service.begin_login(REDIRECT_URI)
    h.script.redeem_result = {
        "error": "access_denied",
        "error_description": f"{EMAIL} {REFRESH_TOKEN}",
    }
    with pytest.raises(AuthFailedError):
        h.service.complete_login(STATE, {"state": STATE, "code": "FAKE-auth-code"})
    h.service.logout()

    for output in (filtered_logs.getvalue(), caplog.text):
        for secret in (ACCESS_TOKEN, REFRESH_TOKEN, EMAIL, "FAKE-auth-code", DISPLAY_NAME):
            assert secret not in output
    assert "login concluído" in filtered_logs.getvalue()
