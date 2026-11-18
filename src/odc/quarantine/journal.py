# SPDX-License-Identifier: GPL-3.0-only
"""Diário de operações sobre arquivos do utilizador (RN-22, plan 2.5 e 3.1).

Protocolo de cada operação (mover, restaurar, excluir, garantir pasta):

1. `Journal.intent(...)` grava `phase=intent` e faz commit **antes** de qualquer chamada à Graph
   (devolve o `op_id`, UUID v4). Nenhuma transação fica aberta durante a chamada externa.
2. A chamada à Graph acontece fora do banco.
3. `Journal.done(op_id, effects)` ou `Journal.failed(op_id, error_code, effects)` aplica os
   efeitos locais (estado do item, `quarantine_entry`) **na mesma transação** que grava a fase.

Uma queda entre 1 e 3 deixa a intenção sem resultado; o `Reconciler` consulta o estado real no
OneDrive e a resolve (`phase=reconciled`). O contexto necessário para isso (modo e caminho
original do mover, motivo da restauração) vai junto com a intenção em `journal.context` (JSON
validado por schema na leitura). Entradas do diário nunca são apagadas.

Falha ambígua da Graph (rede, indisponível, interrupção) NÃO deve virar `failed`: a operação
pode ter sido executada no servidor. Nesses casos o chamador deixa a intenção pendente para a
reconciliação; `failed` é só para resultado conhecido (404, 409 esgotado, 412).

Nomes de arquivo e caminhos nunca vão para logs nem telemetria.
"""

import asyncio
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from odc.storage.repositories import items, journal, quarantine
from odc.storage.uow import UnitOfWork
from odc.telemetry.metrics import JournalOperation
from odc.telemetry.observables import Sample

MAX_PATH_LENGTH: Final = 4096
ERROR_CODE_PATTERN: Final = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

type QuarantineMode = Literal["manual", "auto"]
type RestoreReason = Literal["false_positive", "reclassified", "manual"]
type ItemState = Literal[
    "DESCOBERTO",
    "ANALISADO",
    "FALHA_ANALISE",
    "SUGERIDO",
    "QUARENTENA",
    "RESTAURADO",
    "EXCLUIDO",
    "REMOVIDO_EXTERNAMENTE",
]
type ClosedStatus = Literal["restored", "deleted", "external_removed"]


class Operation(StrEnum):
    """Operações registradas no diário (`journal.operation`)."""

    MOVE = "move"
    RESTORE = "restore"
    DELETE = "delete"
    ENSURE_FOLDER = "ensure_folder"


class JournalError(Exception):
    """Base dos erros do diário; mensagens sem nome de arquivo, caminho ou ID do OneDrive."""


class JournalStateError(JournalError):
    """A operação não existe ou já tem resultado (não está mais em `intent`)."""


class JournalRecordError(JournalError):
    """Registro inválido: campos obrigatórios ausentes ou contexto fora do schema."""


class EffectConflictError(JournalError):
    """Efeito incoerente com o banco (entrada ativa duplicada, inexistente ou item ausente)."""


class EngineSource(Protocol):
    """Quem entrega o `Engine` vigente (muda no login/logout): lido a cada uso."""

    @property
    def engine(self) -> Engine: ...


# --- Contexto da operação (journal.context) ---


class _Context(BaseModel):  # type: ignore[explicit-any]  # BaseModel usa Any
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class MoveContext(_Context):  # type: ignore[explicit-any]  # herda de BaseModel (usa Any)
    """Mover para a quarentena: modo e caminho da pasta original (RN-18, para recriar na RN-19).

    `original_parent_path` vazio significa caminho desconhecido (a restauração usa o fallback).
    """

    kind: Literal["move"] = "move"
    mode: QuarantineMode
    original_parent_path: Annotated[str, Field(max_length=MAX_PATH_LENGTH)]


class RestoreContext(_Context):  # type: ignore[explicit-any]  # herda de BaseModel (usa Any)
    """Restaurar da quarentena: motivo (`false_positive`, `reclassified`, `manual`)."""

    kind: Literal["restore"] = "restore"
    reason: RestoreReason


type OperationContext = MoveContext | RestoreContext

_CONTEXT_ADAPTER: Final[TypeAdapter[OperationContext]] = TypeAdapter(
    Annotated[MoveContext | RestoreContext, Field(discriminator="kind")]
)


def parse_context(raw: str | None) -> OperationContext | None:
    """Valida o JSON de `journal.context` (Zero Trust na leitura do banco)."""
    if raw is None:
        return None
    try:
        return _CONTEXT_ADAPTER.validate_json(raw)
    except ValidationError:
        raise JournalRecordError("contexto de operação inválido") from None


# --- Efeitos aplicados junto com o resultado ---


@dataclass(frozen=True, slots=True)
class SetItemState:
    item_id: int
    state: ItemState


@dataclass(frozen=True, slots=True)
class SetItemLocation:
    """Pasta e nome atuais do item no OneDrive."""

    item_id: int
    parent_id: str
    name: str


@dataclass(frozen=True, slots=True)
class OpenQuarantineEntry:
    """Entrada `active` com a origem registrada (RN-18); no máximo uma ativa por item."""

    item_id: int
    original_parent_id: str
    original_parent_path: str
    original_name: str
    quarantined_name: str
    mode: QuarantineMode


@dataclass(frozen=True, slots=True)
class CloseQuarantineEntry:
    """Encerra a entrada ativa do item (`restored` exige `reason`)."""

    item_id: int
    status: ClosedStatus
    reason: RestoreReason | None = None
    fallback: bool = False


type Effect = SetItemState | SetItemLocation | OpenQuarantineEntry | CloseQuarantineEntry


def apply_effects(session: Session, effects: Sequence[Effect], *, now: str) -> None:
    """Aplica os efeitos na sessão (transação do chamador); incoerência → `EffectConflictError`."""
    for effect in effects:
        match effect:
            case SetItemState(item_id=item_id, state=state):
                _require_item(session, item_id)
                items.set_state(session, item_id, state, now=now)
            case SetItemLocation(item_id=item_id, parent_id=parent_id, name=name):
                _require_item(session, item_id)
                items.set_location(session, item_id, parent_id=parent_id, name=name, now=now)
            case OpenQuarantineEntry():
                _open_entry(session, effect, now=now)
            case _:  # CloseQuarantineEntry (união exaustiva)
                _close_entry(session, effect, now=now)


def _require_item(session: Session, item_id: int) -> None:
    if items.get(session, item_id) is None:
        raise EffectConflictError("item inexistente")


def _open_entry(session: Session, effect: OpenQuarantineEntry, *, now: str) -> None:
    _require_item(session, effect.item_id)
    if not effect.original_parent_id or not effect.original_name or not effect.quarantined_name:
        raise EffectConflictError("entrada de quarentena sem origem ou nome")
    if quarantine.get_active_by_item(session, effect.item_id) is not None:
        raise EffectConflictError("item já tem entrada de quarentena ativa")
    quarantine.create(
        session,
        item_id=effect.item_id,
        original_parent_id=effect.original_parent_id,
        original_parent_path=effect.original_parent_path,
        original_name=effect.original_name,
        quarantined_name=effect.quarantined_name,
        mode=effect.mode,
        now=now,
    )


def _close_entry(session: Session, effect: CloseQuarantineEntry, *, now: str) -> None:
    entry = quarantine.get_active_by_item(session, effect.item_id)
    if entry is None:
        raise EffectConflictError("item sem entrada de quarentena ativa")
    if effect.status == "restored":
        if effect.reason is None:
            raise EffectConflictError("restauração sem motivo")
        quarantine.mark_restored(
            session, entry.id, reason=effect.reason, fallback=effect.fallback, now=now
        )
    elif effect.status == "deleted":
        quarantine.mark_deleted(session, entry.id, now=now)
    else:
        quarantine.mark_external_removed(session, entry.id)


# --- Diário ---


def utc_now() -> datetime:
    return datetime.now(UTC)


def stamp(moment: datetime) -> str:
    """ISO-8601 UTC de largura fixa: a ordem lexicográfica é a cronológica."""
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def checked_error_code(value: str) -> str:
    """`error_code` no formato MAIUSCULAS_COM_UNDERSCORE (sem dado do utilizador)."""
    if not ERROR_CODE_PATTERN.fullmatch(value):
        raise ValueError("error_code inválido (esperado MAIUSCULAS_COM_UNDERSCORE)")
    return value


@dataclass(frozen=True, slots=True)
class _Intent:
    operation: Operation
    item_id: int | None
    from_parent: str | None
    to_parent: str | None
    from_name: str | None
    to_name: str | None
    context: OperationContext | None

    def validate(self) -> None:
        """Campos exigidos por operação: sem eles a reconciliação não teria como decidir."""
        required: tuple[object, ...]
        expected: type[_Context] | None = None
        match self.operation:
            case Operation.MOVE:
                required = (self.item_id, self.from_parent, self.to_parent, self.from_name)
                expected = MoveContext
            case Operation.RESTORE:
                required = (self.item_id, self.from_parent, self.to_parent)
                expected = RestoreContext
            case Operation.DELETE:
                required = (self.item_id,)
            case _:  # Operation.ENSURE_FOLDER
                required = (self.to_parent, self.to_name)
        if any(value is None or value == "" for value in required):
            raise JournalRecordError(f"campos obrigatórios ausentes para '{self.operation}'")
        if expected is None and self.context is not None:
            raise JournalRecordError(f"'{self.operation}' não aceita contexto")
        if expected is not None and not isinstance(self.context, expected):
            raise JournalRecordError(f"'{self.operation}' exige contexto {expected.__name__}")


def new_op_id() -> str:
    """`op_id` da operação: UUID v4 (idempotência)."""
    return str(uuid.uuid4())


class Journal:
    """Registro de intenção e resultado das operações (assíncrono; banco em thread)."""

    def __init__(
        self,
        database: EngineSource,
        *,
        clock: Callable[[], datetime] = utc_now,
        op_ids: Callable[[], str] = new_op_id,
    ) -> None:
        self._database = database
        self._clock = clock
        self._op_ids = op_ids

    async def intent(  # noqa: PLR0913 - campos do registro de intenção (plan 3.1)
        self,
        operation: Operation,
        item_id: int | None,
        *,
        from_parent: str | None = None,
        to_parent: str | None = None,
        from_name: str | None = None,
        to_name: str | None = None,
        context: OperationContext | None = None,
    ) -> str:
        """Grava `phase=intent` com commit imediato e devolve o `op_id` (antes da Graph)."""
        record = _Intent(
            Operation(operation), item_id, from_parent, to_parent, from_name, to_name, context
        )
        record.validate()
        op_id = self._op_ids()
        await asyncio.to_thread(self._write_intent, op_id, record)
        return op_id

    async def done(self, op_id: str, effects: Sequence[Effect] = ()) -> None:
        """Resultado `done` + efeitos na mesma transação (tudo ou nada)."""
        await asyncio.to_thread(self._finish, op_id, "done", None, tuple(effects))

    async def failed(self, op_id: str, error_code: str, effects: Sequence[Effect] = ()) -> None:
        """Resultado `failed(error_code)` + efeitos (ex.: item apagado → removido externamente)."""
        code = checked_error_code(error_code)
        await asyncio.to_thread(self._finish, op_id, "failed", code, tuple(effects))

    def _now(self) -> str:
        return stamp(self._clock())

    def _write_intent(self, op_id: str, record: _Intent) -> None:
        context = None if record.context is None else record.context.model_dump_json()
        with UnitOfWork(self._database.engine) as session:
            journal.begin(
                session,
                op_id=op_id,
                operation=record.operation.value,
                now=self._now(),
                item_id=record.item_id,
                from_parent_id=record.from_parent,
                to_parent_id=record.to_parent,
                from_name=record.from_name,
                to_name=record.to_name,
                context=context,
            )

    def _finish(
        self, op_id: str, phase: str, error_code: str | None, effects: tuple[Effect, ...]
    ) -> None:
        now = self._now()
        with UnitOfWork(self._database.engine) as session:
            if not journal.finish(session, op_id, phase, now=now, error_code=error_code):
                raise JournalStateError("operação inexistente ou já encerrada")
            apply_effects(session, effects, now=now)


class JournalPendingSource:
    """Gauge `odc.journal.pending`: intenções sem resultado por operação (com zeros)."""

    def __init__(self, database: EngineSource) -> None:
        self._database = database

    def collect(self) -> Sequence[Sample]:
        with UnitOfWork(self._database.engine, write=False) as session:
            counts = journal.count_pending_by_operation(session)
        return [
            Sample(float(counts.get(op.value, 0)), {"operation": op}) for op in JournalOperation
        ]


__all__ = [
    "CloseQuarantineEntry",
    "Effect",
    "EffectConflictError",
    "EngineSource",
    "ItemState",
    "Journal",
    "JournalError",
    "JournalPendingSource",
    "JournalRecordError",
    "JournalStateError",
    "MoveContext",
    "OpenQuarantineEntry",
    "Operation",
    "OperationContext",
    "RestoreContext",
    "SetItemLocation",
    "SetItemState",
    "apply_effects",
    "checked_error_code",
    "new_op_id",
    "parse_context",
    "stamp",
    "utc_now",
]
