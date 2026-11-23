# SPDX-License-Identifier: GPL-3.0-only
import subprocess
import sys
import types
from pathlib import Path
from typing import ClassVar

import pytest

from odc.account_key import account_key
from odc.auth import token_cache
from odc.auth.errors import KeychainUnavailableError
from odc.auth.token_cache import (
    ACTIVE_ACCOUNT_NAME,
    EMPTY_CONTENT,
    KEYCHAIN_SERVICE,
    KeychainTokenStore,
    build_token_cache,
    read_entry,
    write_entry,
)

from .fakes import MemoryTokenStore

KEY = account_key("conta-de-teste")


class FakeKeychainError(OSError):
    ITEM_NOT_FOUND = -25300

    def __init__(self, exit_status: int) -> None:
        super().__init__()
        self.exit_status = exit_status


class FakeKeychain:
    """Imita `msal_extensions.osx.Keychain` com um dicionário (nunca toca o Keychain real)."""

    items: ClassVar[dict[tuple[str, str], str]] = {}
    failure: ClassVar[int | None] = None

    def __enter__(self) -> "FakeKeychain":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def get_generic_password(self, service: str, account_name: str) -> str:
        if self.failure is not None:
            raise FakeKeychainError(self.failure)
        try:
            return self.items[(service, account_name)]
        except KeyError:
            raise FakeKeychainError(FakeKeychainError.ITEM_NOT_FOUND) from None

    def set_generic_password(self, service: str, account_name: str, value: str) -> None:
        if self.failure is not None:
            raise FakeKeychainError(self.failure)
        self.items[(service, account_name)] = value


@pytest.fixture
def keychain(monkeypatch: pytest.MonkeyPatch) -> type[FakeKeychain]:
    FakeKeychain.items = {}
    FakeKeychain.failure = None
    module = types.ModuleType("msal_extensions.osx")
    module.Keychain = FakeKeychain  # type: ignore[attr-defined]
    module.KeychainError = FakeKeychainError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "msal_extensions.osx", module)
    return FakeKeychain


@pytest.fixture
def security_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        assert kwargs["check"] is False
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_persistencia_grava_e_le_no_keychain_com_servico_e_conta(
    tmp_path: Path, keychain: type[FakeKeychain]
) -> None:
    store = KeychainTokenStore(tmp_path)

    persistence = store.persistence(KEY)
    write_entry(persistence, '{"RefreshToken": {}}')

    assert keychain.items == {(KEYCHAIN_SERVICE, KEY): '{"RefreshToken": {}}'}
    assert read_entry(store.persistence(KEY)) == '{"RefreshToken": {}}'
    assert persistence.get_location() == str(tmp_path / f"{KEY}.signal")


def test_nenhum_arquivo_de_token_e_criado_em_disco(
    tmp_path: Path, keychain: type[FakeKeychain]
) -> None:
    write_entry(KeychainTokenStore(tmp_path).persistence(KEY), "SEGREDO-FAKE")

    for file in tmp_path.rglob("*"):
        if file.is_file():
            assert "SEGREDO-FAKE" not in file.read_text()


def test_diretorio_padrao_fica_em_auth_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    keychain: type[FakeKeychain],
    macos_platformdirs: None,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    location = KeychainTokenStore().persistence(ACTIVE_ACCOUNT_NAME).get_location()

    expected = tmp_path / "Library" / "Application Support" / "OneDriveCleaner" / "auth"
    assert Path(location) == expected / f"{ACTIVE_ACCOUNT_NAME}.signal"


@pytest.mark.parametrize("name", ["", "../x", KEY.upper(), KEY[:-1], "outra-conta", f"{KEY}/x"])
def test_nome_de_entrada_invalido_e_recusado(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="inválido"):
        KeychainTokenStore(tmp_path).persistence(name)


def test_entrada_inexistente_le_como_none(tmp_path: Path, keychain: type[FakeKeychain]) -> None:
    assert read_entry(KeychainTokenStore(tmp_path).persistence(KEY)) is None


@pytest.mark.parametrize("content", ["", EMPTY_CONTENT])
def test_entrada_limpa_le_como_none(
    tmp_path: Path, keychain: type[FakeKeychain], content: str
) -> None:
    persistence = KeychainTokenStore(tmp_path).persistence(KEY)
    write_entry(persistence, content)

    assert read_entry(persistence) is None


def test_erro_do_keychain_na_leitura_e_explicito(
    tmp_path: Path, keychain: type[FakeKeychain]
) -> None:
    persistence = KeychainTokenStore(tmp_path).persistence(KEY)
    keychain.failure = -128

    with pytest.raises(KeychainUnavailableError, match="ler"):
        read_entry(persistence)


def test_erro_do_keychain_na_gravacao_e_explicito(
    tmp_path: Path, keychain: type[FakeKeychain]
) -> None:
    persistence = KeychainTokenStore(tmp_path).persistence(KEY)
    keychain.failure = -128

    with pytest.raises(KeychainUnavailableError, match="gravar"):
        write_entry(persistence, "x")


def test_keychain_ausente_no_sistema_nao_tem_fallback_para_arquivo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "msal_extensions.osx", None)

    with pytest.raises(KeychainUnavailableError, match="indisponível"):
        KeychainTokenStore(tmp_path).persistence(KEY)

    assert list(tmp_path.iterdir()) == []


def test_falha_de_oserror_ao_abrir_o_keychain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise OSError(-25307)

    monkeypatch.setattr(token_cache, "KeychainPersistence", broken)

    with pytest.raises(KeychainUnavailableError):
        KeychainTokenStore(tmp_path).persistence(KEY)


def test_delete_sobrescreve_e_remove_a_entrada_pelo_security(
    tmp_path: Path, keychain: type[FakeKeychain], security_calls: list[list[str]]
) -> None:
    store = KeychainTokenStore(tmp_path)
    write_entry(store.persistence(KEY), "SEGREDO-FAKE")

    store.delete(KEY)

    assert keychain.items[(KEYCHAIN_SERVICE, KEY)] == EMPTY_CONTENT
    assert security_calls == [
        ["/usr/bin/security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEY]
    ]


def test_delete_com_keychain_travado_e_explicito(
    tmp_path: Path, keychain: type[FakeKeychain], security_calls: list[list[str]]
) -> None:
    keychain.failure = -128

    with pytest.raises(KeychainUnavailableError):
        KeychainTokenStore(tmp_path).delete(KEY)

    assert security_calls == []


def test_build_token_cache_persiste_e_recarrega_pelo_msal_extensions(tmp_path: Path) -> None:
    store = MemoryTokenStore(tmp_path)
    cache = build_token_cache(store.persistence(KEY))
    cache.add(
        {
            "client_id": "cid",
            "scope": ["Files.ReadWrite"],
            "token_endpoint": "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
            "response": {
                "access_token": "FAKE-at",
                "refresh_token": "FAKE-rt",
                "expires_in": 3600,
                "scope": "Files.ReadWrite",
                "token_type": "Bearer",
            },
        }
    )

    assert "FAKE-rt" in store.entries[KEY]
    reloaded = build_token_cache(store.persistence(KEY))
    assert [item["secret"] for item in reloaded.search("RefreshToken")] == ["FAKE-rt"]
