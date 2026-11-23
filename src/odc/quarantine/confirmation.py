# SPDX-License-Identifier: GPL-3.0-only
"""Confirmação explícita da exclusão (RN-20, plan 2.5 e 4.1): token de uso único.

Fluxo: `issue` (preview) → utilizador confirma quantidade e volume → `consume` (rota de
confirmação) → job `delete` com o `confirmation_id` → `DeleteHandler` verifica a autorização
(`is_granted`) e a libera ao terminar (`release`).

- O token é aleatório (256 bits), vale 5 minutos e fica vinculado ao conjunto **exato** de itens
  (hash dos IDs ordenados) e à sessão que o pediu. Só o hash SHA-256 do token é guardado.
- `consume` é de uso único: qualquer tentativa que encontre o token o invalida, mesmo com
  conjunto ou sessão diferentes. Token expirado, reutilizado ou de outro conjunto → `False`.
- O `confirmation_id` do job é derivado do token (SHA-256), nunca o token; a autorização
  consumida fica em memória por até 1 hora (janela para o job executar, inclusive após pausa).
  Reiniciar o processo descarta tokens e autorizações: nada é excluído sem nova confirmação.
- Só itens `QUARENTENA` com entrada ativa podem receber token (`NotInQuarantineError`).

Estado em memória, protegido por lock (rotas e runner rodam em threads/loops distintos).
"""

import hashlib
import hmac
import secrets
import threading
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from odc.quarantine.journal import EngineSource, utc_now
from odc.storage.repositories import items, quarantine
from odc.storage.uow import UnitOfWork

TOKEN_TTL: Final = timedelta(minutes=5)
GRANT_TTL: Final = timedelta(hours=1)
MAX_ITEMS: Final = 5000
MAX_PENDING: Final = 256
TOKEN_BYTES: Final = 32


class ConfirmationError(Exception):
    """Base dos erros de confirmação; mensagens sem dado do utilizador."""


class NotInQuarantineError(ConfirmationError):
    """Algum item do conjunto não está na quarentena (RN-20): nenhum token é emitido."""

    def __init__(self, item_ids: tuple[int, ...]) -> None:
        self.item_ids = item_ids
        super().__init__("itens fora da quarentena não podem ser excluídos")


@dataclass(frozen=True, slots=True)
class Confirmation:
    """Resultado do preview: o que o utilizador precisa ver e confirmar (quantidade e volume)."""

    token: str
    count: int
    total_bytes: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class _Record:
    """Token pendente (ou autorização consumida) vinculado ao conjunto e à sessão."""

    set_hash: str
    session_hash: str
    expires_at: datetime


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def set_digest(item_ids: Collection[int]) -> str:
    """Hash do conjunto ordenado e sem repetições de IDs (a ordem do pedido não importa)."""
    return _digest(",".join(str(item_id) for item_id in sorted(set(item_ids))))


class ConfirmationStore:
    """Emite, consome e autoriza confirmações de exclusão. O banco é lido a cada `issue`."""

    def __init__(
        self,
        database: EngineSource,
        *,
        clock: Callable[[], datetime] = utc_now,
        token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(TOKEN_BYTES),
    ) -> None:
        self._database = database
        self._clock = clock
        self._token_factory = token_factory
        self._lock = threading.Lock()
        self._pending: dict[str, _Record] = {}
        self._grants: dict[str, _Record] = {}

    @staticmethod
    def confirmation_id(token: str) -> str:
        """Identificador do token para o payload do job (hash: o token nunca é persistido)."""
        return _digest(token)

    def issue(self, item_ids: Collection[int], session: str) -> Confirmation:
        """Emite o token para o conjunto (somente itens `QUARENTENA`) e a sessão.

        `ValueError` se o conjunto for vazio ou passar de 5000 itens;
        `NotInQuarantineError` se algum item não está na quarentena.
        """
        unique = sorted(set(item_ids))
        if not unique or len(unique) > MAX_ITEMS:
            raise ValueError(f"o conjunto deve ter de 1 a {MAX_ITEMS} itens")
        total_bytes = self._quarantined_bytes(unique)
        token = self._token_factory()
        expires_at = self._clock() + TOKEN_TTL
        record = _Record(set_digest(unique), _digest(session), expires_at)
        with self._lock:
            self._purge()
            if len(self._pending) >= MAX_PENDING:
                del self._pending[next(iter(self._pending))]  # o mais antigo
            self._pending[self.confirmation_id(token)] = record
        return Confirmation(token, len(unique), total_bytes, expires_at)

    def consume(self, token: str, item_ids: Collection[int], session: str) -> bool:
        """Consome o token (uso único). `True` só se válido, do mesmo conjunto e da mesma sessão.

        Em caso de sucesso registra a autorização que o `DeleteHandler` verifica; qualquer
        tentativa que encontre o token o invalida, inclusive as recusadas.
        """
        confirmation_id = self.confirmation_id(token)
        with self._lock:
            self._purge()
            record = self._pending.pop(confirmation_id, None)
            if record is None or not _matches(record, item_ids, session):
                return False
            self._grants[confirmation_id] = _Record(
                record.set_hash, record.session_hash, self._clock() + GRANT_TTL
            )
            return True

    def is_granted(self, confirmation_id: str, item_ids: Collection[int]) -> bool:
        """A autorização existe, não expirou e cobre exatamente este conjunto de itens."""
        with self._lock:
            self._purge()
            grant = self._grants.get(confirmation_id)
            if grant is None:
                return False
            return hmac.compare_digest(grant.set_hash, set_digest(item_ids))

    def release(self, confirmation_id: str) -> None:
        """Descarta a autorização (o job terminou); idempotente."""
        with self._lock:
            self._grants.pop(confirmation_id, None)

    # --- Internos ---

    def _purge(self) -> None:
        """Remove tokens e autorizações vencidos (chamar com o lock tomado)."""
        now = self._clock()
        for registry in (self._pending, self._grants):
            for key in [key for key, record in registry.items() if now >= record.expires_at]:
                del registry[key]

    def _quarantined_bytes(self, item_ids: list[int]) -> int:
        """Volume do conjunto; todo item precisa estar `QUARENTENA` com entrada ativa."""
        rejected: list[int] = []
        total = 0
        with UnitOfWork(self._database.engine, write=False) as session:
            for item_id in item_ids:
                found = items.get(session, item_id)
                if (
                    found is None
                    or found.state != "QUARENTENA"
                    or quarantine.get_active_by_item(session, item_id) is None
                ):
                    rejected.append(item_id)
                else:
                    total += found.size_bytes
        if rejected:
            raise NotInQuarantineError(tuple(rejected))
        return total


def _matches(record: _Record, item_ids: Collection[int], session: str) -> bool:
    same_set = hmac.compare_digest(record.set_hash, set_digest(item_ids))
    same_session = hmac.compare_digest(record.session_hash, _digest(session))
    return same_set and same_session


__all__ = [
    "GRANT_TTL",
    "MAX_ITEMS",
    "MAX_PENDING",
    "TOKEN_TTL",
    "Confirmation",
    "ConfirmationError",
    "ConfirmationStore",
    "NotInQuarantineError",
    "set_digest",
]
