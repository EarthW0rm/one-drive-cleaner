# SPDX-License-Identifier: GPL-3.0-only
"""Token de confirmação de exclusão: TTL com relógio injetado, uso único, conjunto e sessão."""

from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from odc.quarantine.confirmation import (
    GRANT_TTL,
    MAX_ITEMS,
    MAX_PENDING,
    TOKEN_TTL,
    ConfirmationStore,
    NotInQuarantineError,
    set_digest,
)
from tests.unit.quarantine.support import quarantined
from tests.unit.scanning.support import BASE, Database, add_item, add_root, migrated

SESSION = "sessao-a"


class _Now:
    """Relógio manual: só avança quando o teste manda."""

    def __init__(self) -> None:
        self.now = BASE

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class _Tokens:
    """Fábrica de tokens previsíveis (o teste nunca depende de aleatoriedade)."""

    def __init__(self) -> None:
        self.issued = 0

    def __call__(self) -> str:
        self.issued += 1
        return f"FAKE-TOKEN-{self.issued}"


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = migrated(tmp_path)
    yield database
    database.engine.dispose()


@pytest.fixture
def clock() -> _Now:
    return _Now()


@pytest.fixture
def store(db: Database, clock: _Now) -> ConfirmationStore:
    return ConfirmationStore(db, clock=clock, token_factory=_Tokens())


def _quarantine_ids(db: Database, count: int) -> list[int]:
    """Itens `QUARENTENA` (10 bytes cada) com entrada ativa."""
    root = add_root(db, "FAKE-ROOT")
    ids: list[int] = []
    for index in range(count):
        created = add_item(db, root.id, f"FAKE-ITEM-{index}", state="SUGERIDO")
        quarantined(db, created.id)
        ids.append(created.id)
    return ids


# --- issue ---


def test_emite_token_com_quantidade_volume_e_validade_de_5_minutos(
    db: Database, store: ConfirmationStore, clock: _Now
) -> None:
    ids = _quarantine_ids(db, 3)

    issued = store.issue(ids, SESSION)

    assert issued.token.startswith("FAKE-TOKEN-")
    assert (issued.count, issued.total_bytes) == (3, 30)
    assert issued.expires_at == clock.now + TOKEN_TTL
    assert timedelta(minutes=5) == TOKEN_TTL


def test_conjunto_com_repeticoes_conta_cada_item_uma_vez(
    db: Database, store: ConfirmationStore
) -> None:
    (first, second) = _quarantine_ids(db, 2)

    issued = store.issue([second, first, first], SESSION)

    assert (issued.count, issued.total_bytes) == (2, 20)


def test_token_real_tem_256_bits_e_e_diferente_a_cada_emissao(db: Database, clock: _Now) -> None:
    real = ConfirmationStore(db, clock=clock)
    ids = _quarantine_ids(db, 1)

    first, second = real.issue(ids, SESSION), real.issue(ids, SESSION)

    assert first.token != second.token
    assert len(first.token) >= 43  # 32 bytes em base64 url-safe sem padding


@pytest.mark.parametrize("state", ["SUGERIDO", "RESTAURADO", "EXCLUIDO", "DESCOBERTO"])
def test_item_fora_da_quarentena_nao_recebe_token(
    db: Database, store: ConfirmationStore, state: str
) -> None:
    root = add_root(db, "FAKE-ROOT")
    outside = add_item(db, root.id, "FAKE-OUT", state=state)

    with pytest.raises(NotInQuarantineError) as raised:
        store.issue([outside.id], SESSION)

    assert raised.value.item_ids == (outside.id,)
    assert "FAKE-OUT" not in str(raised.value)


def test_quarentena_sem_entrada_ativa_nao_recebe_token(
    db: Database, store: ConfirmationStore
) -> None:
    root = add_root(db, "FAKE-ROOT")
    orphan = add_item(db, root.id, "FAKE-ORPHAN", state="QUARENTENA")

    with pytest.raises(NotInQuarantineError):
        store.issue([orphan.id], SESSION)


def test_um_item_fora_da_quarentena_recusa_o_conjunto_inteiro(
    db: Database, store: ConfirmationStore
) -> None:
    (inside,) = _quarantine_ids(db, 1)
    root = add_root(db, "FAKE-ROOT-2")
    outside = add_item(db, root.id, "FAKE-OUT", state="SUGERIDO")

    with pytest.raises(NotInQuarantineError) as raised:
        store.issue([inside, outside.id, 987654], SESSION)

    assert raised.value.item_ids == (outside.id, 987654)


@pytest.mark.parametrize("size", [0, MAX_ITEMS + 1])
def test_conjunto_vazio_ou_acima_do_limite_e_recusado(store: ConfirmationStore, size: int) -> None:
    with pytest.raises(ValueError, match="de 1 a 5000"):
        store.issue(list(range(1, size + 1)), SESSION)


def test_limite_de_tokens_pendentes_descarta_o_mais_antigo(
    db: Database, clock: _Now, store: ConfirmationStore
) -> None:
    ids = _quarantine_ids(db, 1)
    tokens = [store.issue(ids, SESSION).token for _ in range(MAX_PENDING + 1)]

    assert store.consume(tokens[0], ids, SESSION) is False
    assert store.consume(tokens[-1], ids, SESSION) is True


# --- consume ---


def test_consome_token_valido_do_mesmo_conjunto_e_sessao(
    db: Database, store: ConfirmationStore
) -> None:
    ids = _quarantine_ids(db, 2)
    token = store.issue(ids, SESSION).token

    assert store.consume(token, ids, SESSION) is True


def test_ordem_e_repeticao_dos_ids_nao_alteram_o_conjunto(
    db: Database, store: ConfirmationStore
) -> None:
    (first, second) = _quarantine_ids(db, 2)
    token = store.issue([first, second], SESSION).token

    assert store.consume(token, [second, first, second], SESSION) is True


def test_token_usado_duas_vezes_falha_na_segunda(db: Database, store: ConfirmationStore) -> None:
    ids = _quarantine_ids(db, 1)
    token = store.issue(ids, SESSION).token

    assert store.consume(token, ids, SESSION) is True
    assert store.consume(token, ids, SESSION) is False


def test_token_de_5_minutos_mais_1_segundo_falha(
    db: Database, store: ConfirmationStore, clock: _Now
) -> None:
    ids = _quarantine_ids(db, 1)
    token = store.issue(ids, SESSION).token
    clock.advance(TOKEN_TTL + timedelta(seconds=1))

    assert store.consume(token, ids, SESSION) is False


def test_token_um_segundo_antes_do_vencimento_ainda_vale(
    db: Database, store: ConfirmationStore, clock: _Now
) -> None:
    ids = _quarantine_ids(db, 1)
    token = store.issue(ids, SESSION).token
    clock.advance(TOKEN_TTL - timedelta(seconds=1))

    assert store.consume(token, ids, SESSION) is True


def test_token_no_instante_exato_do_vencimento_falha(
    db: Database, store: ConfirmationStore, clock: _Now
) -> None:
    ids = _quarantine_ids(db, 1)
    token = store.issue(ids, SESSION).token
    clock.advance(TOKEN_TTL)

    assert store.consume(token, ids, SESSION) is False


def test_conjunto_diferente_falha_e_invalida_o_token(
    db: Database, store: ConfirmationStore
) -> None:
    (first, second) = _quarantine_ids(db, 2)
    token = store.issue([first], SESSION).token

    assert store.consume(token, [first, second], SESSION) is False
    assert store.consume(token, [first], SESSION) is False  # já invalidado


def test_subconjunto_do_pedido_tambem_falha(db: Database, store: ConfirmationStore) -> None:
    (first, second) = _quarantine_ids(db, 2)
    token = store.issue([first, second], SESSION).token

    assert store.consume(token, [first], SESSION) is False


def test_sessao_diferente_falha_e_invalida_o_token(db: Database, store: ConfirmationStore) -> None:
    ids = _quarantine_ids(db, 1)
    token = store.issue(ids, SESSION).token

    assert store.consume(token, ids, "sessao-b") is False
    assert store.consume(token, ids, SESSION) is False


def test_token_desconhecido_falha(db: Database, store: ConfirmationStore) -> None:
    ids = _quarantine_ids(db, 1)

    assert store.consume("FAKE-NUNCA-EMITIDO", ids, SESSION) is False


def test_tokens_de_conjuntos_distintos_sao_independentes(
    db: Database, store: ConfirmationStore
) -> None:
    (first, second) = _quarantine_ids(db, 2)
    token_a = store.issue([first], SESSION).token
    token_b = store.issue([second], SESSION).token

    assert store.consume(token_b, [second], SESSION) is True
    assert store.consume(token_a, [first], SESSION) is True


# --- autorização do job ---


def test_autorizacao_cobre_exatamente_o_conjunto_consumido(
    db: Database, store: ConfirmationStore
) -> None:
    (first, second) = _quarantine_ids(db, 2)
    token = store.issue([first, second], SESSION).token
    confirmation_id = store.confirmation_id(token)

    assert store.is_granted(confirmation_id, [first, second]) is False  # ainda não consumido
    assert store.consume(token, [first, second], SESSION) is True

    assert store.is_granted(confirmation_id, [second, first]) is True
    assert store.is_granted(confirmation_id, [first]) is False
    assert store.is_granted(confirmation_id, [first, second, 999]) is False


def test_confirmation_id_e_hash_do_token_e_nao_o_token(store: ConfirmationStore) -> None:
    confirmation_id = store.confirmation_id("FAKE-TOKEN-1")

    assert confirmation_id != "FAKE-TOKEN-1"
    assert len(confirmation_id) == 64
    assert confirmation_id == store.confirmation_id("FAKE-TOKEN-1")


def test_token_nao_consumido_nao_autoriza_o_job(db: Database, store: ConfirmationStore) -> None:
    ids = _quarantine_ids(db, 1)
    token = store.issue(ids, SESSION).token

    assert store.is_granted(store.confirmation_id(token), ids) is False


def test_autorizacao_expira_apos_a_janela_do_job(
    db: Database, store: ConfirmationStore, clock: _Now
) -> None:
    ids = _quarantine_ids(db, 1)
    token = store.issue(ids, SESSION).token
    assert store.consume(token, ids, SESSION) is True
    clock.advance(GRANT_TTL - timedelta(seconds=1))
    assert store.is_granted(store.confirmation_id(token), ids) is True

    clock.advance(timedelta(seconds=1))

    assert store.is_granted(store.confirmation_id(token), ids) is False


def test_release_descarta_a_autorizacao_e_e_idempotente(
    db: Database, store: ConfirmationStore
) -> None:
    ids = _quarantine_ids(db, 1)
    token = store.issue(ids, SESSION).token
    store.consume(token, ids, SESSION)
    confirmation_id = store.confirmation_id(token)

    store.release(confirmation_id)
    store.release(confirmation_id)

    assert store.is_granted(confirmation_id, ids) is False


def test_set_digest_ignora_ordem_e_repeticao() -> None:
    assert set_digest([3, 1, 2]) == set_digest([1, 2, 2, 3])
    assert set_digest([1, 2]) != set_digest([1, 2, 3])
