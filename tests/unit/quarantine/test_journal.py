# SPDX-License-Identifier: GPL-3.0-only
import json
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from odc.quarantine.journal import (
    CloseQuarantineEntry,
    EffectConflictError,
    Journal,
    JournalPendingSource,
    JournalRecordError,
    JournalStateError,
    MoveContext,
    OpenQuarantineEntry,
    Operation,
    RestoreContext,
    SetItemLocation,
    SetItemState,
    checked_error_code,
    new_op_id,
    parse_context,
)
from odc.storage.dto import ItemRow
from odc.telemetry.metrics import JournalOperation
from tests.unit.quarantine.support import (
    ORIGIN,
    QUARANTINE,
    Clock,
    active_entry,
    entries,
    get_item,
    journal_count,
    journal_row,
)
from tests.unit.scanning.support import Database, add_item, add_root, migrated

MOVE_CTX = MoveContext(mode="manual", original_parent_path="/Imagens/Camera Roll/2024")


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = migrated(tmp_path)
    yield database
    database.engine.dispose()


@pytest.fixture
def item(db: Database) -> ItemRow:
    root = add_root(db, "FAKE-ROOT")
    return add_item(db, root.id, "FAKE-ITEM-1", state="SUGERIDO", parent_id=ORIGIN)


@pytest.fixture
def jr(db: Database) -> Journal:
    return Journal(db, clock=Clock())


def _entry(item: ItemRow, quarantined_name: str = "img (1).jpg") -> OpenQuarantineEntry:
    return OpenQuarantineEntry(
        item_id=item.id,
        original_parent_id=ORIGIN,
        original_parent_path="/Imagens/Camera Roll/2024",
        original_name="img.jpg",
        quarantined_name=quarantined_name,
        mode="manual",
    )


async def _move_intent(jr: Journal, item: ItemRow) -> str:
    return await jr.intent(
        Operation.MOVE,
        item.id,
        from_parent=ORIGIN,
        to_parent=QUARANTINE,
        from_name="img.jpg",
        to_name="img.jpg",
        context=MOVE_CTX,
    )


# --- intent ---


@pytest.mark.asyncio
async def test_intent_grava_e_comita_antes_de_devolver_o_op_id(
    db: Database, jr: Journal, item: ItemRow
) -> None:
    op_id = await _move_intent(jr, item)

    row = journal_row(db, op_id)  # outra conexão: a intenção já está persistida
    assert uuid.UUID(op_id).version == 4
    assert (row.phase, row.operation, row.item_id) == ("intent", "move", item.id)
    assert (row.from_parent_id, row.to_parent_id, row.from_name, row.to_name) == (
        ORIGIN,
        QUARANTINE,
        "img.jpg",
        "img.jpg",
    )
    assert json.loads(row.context or "") == {
        "kind": "move",
        "mode": "manual",
        "original_parent_path": "/Imagens/Camera Roll/2024",
    }


@pytest.mark.asyncio
async def test_intent_de_cada_operacao_com_os_campos_minimos(
    db: Database, jr: Journal, item: ItemRow
) -> None:
    restore = await jr.intent(
        Operation.RESTORE,
        item.id,
        from_parent=QUARANTINE,
        to_parent=ORIGIN,
        context=RestoreContext(reason="false_positive"),
    )
    delete = await jr.intent(Operation.DELETE, item.id)
    ensure = await jr.intent(Operation.ENSURE_FOLDER, None, to_parent="root", to_name="Quarentena")

    assert journal_row(db, restore).context == '{"kind":"restore","reason":"false_positive"}'
    assert (journal_row(db, delete).operation, journal_row(db, delete).context) == (
        "delete",
        None,
    )
    assert journal_row(db, ensure).item_id is None


@pytest.mark.asyncio
async def test_intent_aceita_operacao_em_texto(db: Database, jr: Journal, item: ItemRow) -> None:
    op_id = await jr.intent("delete", item.id)  # type: ignore[arg-type]

    assert journal_row(db, op_id).operation == "delete"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "kwargs"),
    [
        (Operation.MOVE, {"from_parent": ORIGIN, "to_parent": QUARANTINE, "context": MOVE_CTX}),
        (Operation.MOVE, {"from_parent": "", "to_parent": QUARANTINE, "from_name": "a"}),
        (
            Operation.MOVE,
            {"from_parent": ORIGIN, "to_parent": QUARANTINE, "from_name": "a.jpg"},
        ),
        (
            Operation.MOVE,
            {
                "from_parent": ORIGIN,
                "to_parent": QUARANTINE,
                "from_name": "a.jpg",
                "context": RestoreContext(reason="manual"),
            },
        ),
        (Operation.RESTORE, {"from_parent": QUARANTINE, "to_parent": ORIGIN}),
        (Operation.DELETE, {"context": MOVE_CTX}),
        (Operation.ENSURE_FOLDER, {"to_parent": "root"}),
    ],
)
async def test_intent_incompleta_e_recusada_sem_gravar(
    db: Database,
    jr: Journal,
    item: ItemRow,
    operation: Operation,
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(JournalRecordError):
        await jr.intent(operation, item.id, **kwargs)  # type: ignore[arg-type]

    assert journal_count(db) == 0


@pytest.mark.asyncio
async def test_intent_sem_item_e_recusada(db: Database, jr: Journal) -> None:
    with pytest.raises(JournalRecordError):
        await jr.intent(Operation.DELETE, None)

    assert journal_count(db) == 0


@pytest.mark.asyncio
async def test_intent_com_operacao_desconhecida_e_recusada(jr: Journal, item: ItemRow) -> None:
    with pytest.raises(ValueError, match="rename"):
        await jr.intent("rename", item.id)  # type: ignore[arg-type]


# --- done / failed ---


@pytest.mark.asyncio
async def test_done_aplica_efeitos_na_mesma_transacao(
    db: Database, jr: Journal, item: ItemRow
) -> None:
    op_id = await _move_intent(jr, item)

    await jr.done(
        op_id,
        [
            _entry(item),
            SetItemState(item.id, "QUARENTENA"),
            SetItemLocation(item.id, QUARANTINE, "img (1).jpg"),
        ],
    )

    row = journal_row(db, op_id)
    current = get_item(db, item.id)
    entry = active_entry(db, item.id)
    assert (row.phase, row.error_code) == ("done", None)
    assert (current.state, current.parent_id, current.name) == (
        "QUARENTENA",
        QUARANTINE,
        "img (1).jpg",
    )
    assert entry is not None
    assert (entry.original_parent_id, entry.original_name, entry.quarantined_name) == (
        ORIGIN,
        "img.jpg",
        "img (1).jpg",
    )
    assert (entry.original_parent_path, entry.mode) == ("/Imagens/Camera Roll/2024", "manual")


@pytest.mark.asyncio
async def test_done_sem_efeitos(db: Database, jr: Journal, item: ItemRow) -> None:
    op_id = await jr.intent(Operation.DELETE, item.id)

    await jr.done(op_id)

    assert journal_row(db, op_id).phase == "done"


@pytest.mark.asyncio
async def test_efeito_incoerente_desfaz_tudo_e_a_intencao_segue_pendente(
    db: Database, jr: Journal, item: ItemRow
) -> None:
    first = await _move_intent(jr, item)
    await jr.done(first, [_entry(item), SetItemState(item.id, "QUARENTENA")])
    second = await _move_intent(jr, item)

    with pytest.raises(EffectConflictError):
        await jr.done(second, [SetItemState(item.id, "SUGERIDO"), _entry(item)])

    assert journal_row(db, second).phase == "intent"
    assert get_item(db, item.id).state == "QUARENTENA"
    assert len(entries(db, item.id)) == 1


@pytest.mark.asyncio
async def test_done_duas_vezes_e_recusado_sem_reaplicar_efeitos(
    db: Database, jr: Journal, item: ItemRow
) -> None:
    op_id = await _move_intent(jr, item)
    await jr.done(op_id, [_entry(item)])

    with pytest.raises(JournalStateError):
        await jr.done(op_id, [_entry(item)])

    assert len(entries(db, item.id)) == 1


@pytest.mark.asyncio
async def test_done_de_operacao_inexistente(jr: Journal) -> None:
    with pytest.raises(JournalStateError):
        await jr.done(new_op_id())


@pytest.mark.asyncio
async def test_failed_grava_codigo_e_efeitos(db: Database, jr: Journal, item: ItemRow) -> None:
    op_id = await _move_intent(jr, item)

    await jr.failed(op_id, "ITEM_NOT_FOUND", [SetItemState(item.id, "REMOVIDO_EXTERNAMENTE")])

    row = journal_row(db, op_id)
    assert (row.phase, row.error_code) == ("failed", "ITEM_NOT_FOUND")
    assert get_item(db, item.id).state == "REMOVIDO_EXTERNAMENTE"


@pytest.mark.asyncio
async def test_failed_com_codigo_fora_do_formato_nao_grava(
    db: Database, jr: Journal, item: ItemRow
) -> None:
    op_id = await _move_intent(jr, item)

    with pytest.raises(ValueError, match="error_code"):
        await jr.failed(op_id, "nome do arquivo.jpg")

    assert journal_row(db, op_id).phase == "intent"


@pytest.mark.asyncio
async def test_rejected_grava_intencao_e_falha_na_mesma_transacao(
    db: Database, jr: Journal, item: ItemRow
) -> None:
    op_id = await jr.rejected(
        Operation.RESTORE,
        item.id,
        "NOT_IN_QUARANTINE",
        from_parent=QUARANTINE,
        to_parent=ORIGIN,
        context=RestoreContext(reason="manual"),
    )

    row = journal_row(db, op_id)
    assert (row.operation, row.phase, row.error_code) == ("restore", "failed", "NOT_IN_QUARANTINE")
    assert journal_row(db, op_id).context == RestoreContext(reason="manual").model_dump_json()


@pytest.mark.asyncio
async def test_rejected_com_op_id_ja_encerrado_e_recusado(db: Database, item: ItemRow) -> None:
    fixed = Journal(db, clock=Clock(), op_ids=lambda: "op-fixo")
    context = RestoreContext(reason="manual")
    await fixed.rejected(
        Operation.RESTORE,
        item.id,
        "X_ERRO",
        from_parent=QUARANTINE,
        to_parent=ORIGIN,
        context=context,
    )

    with pytest.raises(JournalStateError):
        await fixed.rejected(
            Operation.RESTORE,
            item.id,
            "X_ERRO",
            from_parent=QUARANTINE,
            to_parent=ORIGIN,
            context=context,
        )


@pytest.mark.asyncio
async def test_rejected_com_registro_ou_codigo_invalido_nao_grava(
    db: Database, jr: Journal, item: ItemRow
) -> None:
    with pytest.raises(JournalRecordError):
        await jr.rejected(Operation.RESTORE, item.id, "NOT_IN_QUARANTINE")
    with pytest.raises(ValueError, match="error_code"):
        await jr.rejected(
            Operation.RESTORE,
            item.id,
            "nome do arquivo.jpg",
            from_parent=QUARANTINE,
            to_parent=ORIGIN,
            context=RestoreContext(reason="manual"),
        )

    assert journal_count(db) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "reason", "fallback", "field_values"),
    [
        ("restored", "false_positive", True, ("restored", "false_positive", True)),
        ("deleted", None, False, ("deleted", None, False)),
        ("external_removed", None, False, ("external_removed", None, False)),
    ],
)
async def test_close_encerra_a_entrada_ativa(  # noqa: PLR0913, PLR0917 - tabela de casos
    db: Database,
    jr: Journal,
    item: ItemRow,
    status: str,
    reason: str | None,
    fallback: bool,
    field_values: tuple[str, str | None, bool],
) -> None:
    moved = await _move_intent(jr, item)
    await jr.done(moved, [_entry(item)])
    op_id = await jr.intent(Operation.DELETE, item.id)

    await jr.done(op_id, [CloseQuarantineEntry(item.id, status, reason, fallback)])  # type: ignore[arg-type]

    (entry,) = entries(db, item.id)
    assert (entry.status, entry.restore_reason, entry.restore_fallback) == field_values
    assert active_entry(db, item.id) is None


@pytest.mark.asyncio
async def test_close_sem_entrada_ativa_e_recusado(jr: Journal, item: ItemRow) -> None:
    op_id = await jr.intent(Operation.DELETE, item.id)

    with pytest.raises(EffectConflictError, match="sem entrada"):
        await jr.done(op_id, [CloseQuarantineEntry(item.id, "deleted")])


@pytest.mark.asyncio
async def test_restauracao_sem_motivo_e_recusada(db: Database, jr: Journal, item: ItemRow) -> None:
    moved = await _move_intent(jr, item)
    await jr.done(moved, [_entry(item)])
    op_id = await jr.intent(Operation.DELETE, item.id)

    with pytest.raises(EffectConflictError, match="motivo"):
        await jr.done(op_id, [CloseQuarantineEntry(item.id, "restored")])

    assert active_entry(db, item.id) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "effect",
    [
        SetItemState(999, "QUARENTENA"),
        SetItemLocation(999, QUARANTINE, "a.jpg"),
        OpenQuarantineEntry(999, ORIGIN, "/x", "a.jpg", "a.jpg", "manual"),
    ],
)
async def test_efeito_sobre_item_inexistente(
    jr: Journal, item: ItemRow, effect: SetItemState | SetItemLocation | OpenQuarantineEntry
) -> None:
    op_id = await jr.intent(Operation.DELETE, item.id)

    with pytest.raises(EffectConflictError, match="inexistente"):
        await jr.done(op_id, [effect])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parent", "original", "quarantined"),
    [("", "a.jpg", "a.jpg"), (ORIGIN, "", "a.jpg"), (ORIGIN, "a.jpg", "")],
)
async def test_entrada_sem_origem_ou_nome_e_recusada(  # noqa: PLR0913, PLR0917 - casos
    db: Database, jr: Journal, item: ItemRow, parent: str, original: str, quarantined: str
) -> None:
    op_id = await jr.intent(Operation.DELETE, item.id)
    effect = OpenQuarantineEntry(item.id, parent, "/x", original, quarantined, "manual")

    with pytest.raises(EffectConflictError, match="origem"):
        await jr.done(op_id, [effect])

    assert entries(db, item.id) == []


# --- contexto e utilitários ---


def test_parse_context_valida_o_schema() -> None:
    assert parse_context(None) is None
    assert parse_context(MOVE_CTX.model_dump_json()) == MOVE_CTX
    assert parse_context('{"kind":"restore","reason":"manual"}') == RestoreContext(reason="manual")


@pytest.mark.parametrize(
    "raw",
    [
        "{",
        '{"kind":"move","mode":"x","original_parent_path":"/a"}',
        '{"kind":"restore","reason":"manual","extra":1}',
        '{"kind":"rename"}',
        '{"kind":"move","mode":"auto","original_parent_path":"' + "a" * 4097 + '"}',
    ],
)
def test_parse_context_recusa_registro_adulterado(raw: str) -> None:
    with pytest.raises(JournalRecordError):
        parse_context(raw)


def test_contexto_e_imutavel_e_estrito() -> None:
    with pytest.raises(ValidationError):
        MoveContext(mode="manual", original_parent_path=1)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["NOT_APPLIED", "HTTP_412", "A"])
def test_error_code_valido(value: str) -> None:
    assert checked_error_code(value) == value


@pytest.mark.parametrize("value", ["", "not_applied", "1ABC", "A" * 65])
def test_error_code_invalido(value: str) -> None:
    with pytest.raises(ValueError, match="error_code"):
        checked_error_code(value)


# --- gauge ---


@pytest.mark.asyncio
async def test_gauge_de_pendentes_por_operacao_com_zeros(
    db: Database, jr: Journal, item: ItemRow
) -> None:
    await _move_intent(jr, item)
    await _move_intent(jr, item)
    done = await jr.intent(Operation.DELETE, item.id)
    await jr.done(done)

    samples = JournalPendingSource(db).collect()

    assert {s.labels["operation"]: s.value for s in samples} == {
        JournalOperation.MOVE: 2.0,
        JournalOperation.RESTORE: 0.0,
        JournalOperation.DELETE: 0.0,
        JournalOperation.ENSURE_FOLDER: 0.0,
    }


@pytest.mark.asyncio
async def test_relogio_padrao_grava_instante_utc(db: Database, item: ItemRow) -> None:
    op_id = await Journal(db).intent(Operation.DELETE, item.id)

    assert journal_row(db, op_id).created_at.endswith("+00:00")
