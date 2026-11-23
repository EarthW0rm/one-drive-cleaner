# SPDX-License-Identifier: GPL-3.0-only
"""Injeção declarativa de falhas no Graph fake (plan 10.2).

Cada falha é um valor imutável na descrição, com um contador `times` (quantas vezes dispara;
`None` = persistente até `FaultPlan.clear()`). Os construtores `throttle`, `fail_network`,
`gone_delta`, `expire_token`, `not_found`, `conflict_on_move` e `etag_changed` criam as falhas;
`FaultPlan.add` as registra. O plano é serializável em JSON (endpoint `/__fake__/faults`).
"""

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import ClassVar, Final, cast

from odc.telemetry.metrics import GraphOperation

THROTTLE_STATUSES: Final = frozenset({429, 503, 504})

type Matcher = Callable[[Fault], bool]


@dataclass
class Fault:
    """Base: `times` restantes (`None` = persistente)."""

    kind: ClassVar[str] = ""
    times: int | None = 1

    def __post_init__(self) -> None:
        if self.times is not None and self.times < 1:
            message = "times deve ser >= 1 ou None"
            raise ValueError(message)

    def to_json(self) -> dict[str, object]:
        """Representação JSON (inclui o `kind` e os contadores restantes)."""
        return {"kind": self.kind, "times": self.times}


@dataclass
class Throttle(Fault):
    kind: ClassVar[str] = "throttle"
    op: GraphOperation = GraphOperation.DELTA
    retry_after: float | None = None
    status: int = 429

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.status not in THROTTLE_STATUSES:
            message = "status de throttle deve ser 429, 503 ou 504"
            raise ValueError(message)
        if self.retry_after is not None and self.retry_after < 0:
            message = "retry_after não pode ser negativo"
            raise ValueError(message)

    def to_json(self) -> dict[str, object]:
        return {
            **super().to_json(),
            "op": self.op.value,
            "retry_after": self.retry_after,
            "status": self.status,
        }


@dataclass
class NetworkFailure(Fault):
    kind: ClassVar[str] = "fail_network"
    op: GraphOperation = GraphOperation.DELTA

    def to_json(self) -> dict[str, object]:
        return {**super().to_json(), "op": self.op.value}


@dataclass
class GoneDelta(Fault):
    kind: ClassVar[str] = "gone_delta"
    root: str | None = None

    def to_json(self) -> dict[str, object]:
        return {**super().to_json(), "root": self.root}


@dataclass
class ExpireToken(Fault):
    kind: ClassVar[str] = "expire_token"


@dataclass
class NotFound(Fault):
    kind: ClassVar[str] = "not_found"
    item: str = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.item:
            message = "item é obrigatório"
            raise ValueError(message)

    def to_json(self) -> dict[str, object]:
        return {**super().to_json(), "item": self.item}


@dataclass
class ConflictOnMove(Fault):
    kind: ClassVar[str] = "conflict_on_move"
    name: str = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.name:
            message = "name é obrigatório"
            raise ValueError(message)

    def to_json(self) -> dict[str, object]:
        return {**super().to_json(), "name": self.name}


@dataclass
class EtagChanged(Fault):
    kind: ClassVar[str] = "etag_changed"
    item: str = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.item:
            message = "item é obrigatório"
            raise ValueError(message)

    def to_json(self) -> dict[str, object]:
        return {**super().to_json(), "item": self.item}


def throttle(
    op: GraphOperation, times: int | None = 1, retry_after: float | None = None, status: int = 429
) -> Throttle:
    """As próximas `times` chamadas de `op` respondem 429 (ou 503/504) com `Retry-After`."""
    return Throttle(times=times, op=op, retry_after=retry_after, status=status)


def fail_network(op: GraphOperation, times: int | None = 1) -> NetworkFailure:
    """As próximas `times` chamadas de `op` falham como queda de conexão."""
    return NetworkFailure(times=times, op=op)


def gone_delta(root: str | None = None, times: int | None = 1) -> GoneDelta:
    """A próxima delta incremental (com token) de `root` (ou de qualquer raiz) responde 410."""
    return GoneDelta(times=times, root=root)


def expire_token(times: int | None = 1) -> ExpireToken:
    """As próximas `times` chamadas autenticadas respondem 401 (token expirado)."""
    return ExpireToken(times=times)


def not_found(item: str, times: int | None = None) -> NotFound:
    """Todo acesso a `item` responde 404 (por padrão, até `FaultPlan.clear()`)."""
    return NotFound(times=times, item=item)


def conflict_on_move(name: str, times: int | None = None) -> ConflictOnMove:
    """Mover/renomear para `name` responde 409, mesmo sem colisão real (por padrão, persistente)."""
    return ConflictOnMove(times=times, name=name)


def etag_changed(item: str, times: int | None = 1) -> EtagChanged:
    """Antes da próxima mutação de `item`, o eTag muda (edição externa): `If-Match` falha."""
    return EtagChanged(times=times, item=item)


def fault_from_json(data: Mapping[str, object]) -> Fault:  # noqa: PLR0911 - um retorno por kind
    """Reconstrói uma falha do JSON do endpoint de controle; formato inválido → `ValueError`."""
    kind = data.get("kind")
    times = _times(data.get("times", 1))
    if kind == Throttle.kind:
        return throttle(
            _op(data.get("op")), times, _optional_float(data.get("retry_after")), _status(data)
        )
    if kind == NetworkFailure.kind:
        return fail_network(_op(data.get("op")), times)
    if kind == GoneDelta.kind:
        return gone_delta(_optional_str(data.get("root")), times)
    if kind == ExpireToken.kind:
        return expire_token(times)
    if kind == NotFound.kind:
        return not_found(_required_str(data, "item"), times)
    if kind == ConflictOnMove.kind:
        return conflict_on_move(_required_str(data, "name"), times)
    if kind == EtagChanged.kind:
        return etag_changed(_required_str(data, "item"), times)
    message = "kind de falha desconhecido"
    raise ValueError(message)


def _times(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        message = "times deve ser inteiro ou null"
        raise ValueError(message)
    return value


def _op(value: object) -> GraphOperation:
    try:
        return GraphOperation(str(value))
    except ValueError as error:
        message = "op desconhecida"
        raise ValueError(message) from error


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        message = "retry_after deve ser número ou null"
        raise ValueError(message)
    return float(value)


def _status(data: Mapping[str, object]) -> int:
    value = data.get("status", 429)
    if isinstance(value, bool) or not isinstance(value, int):
        message = "status deve ser inteiro"
        raise ValueError(message)
    return value


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        message = "valor deve ser texto ou null"
        raise ValueError(message)
    return value


def _required_str(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        message = f"{key} é obrigatório"
        raise ValueError(message)
    return value


class FaultPlan:
    """Conjunto de falhas pendentes, consultado a cada requisição (thread-safe)."""

    def __init__(self, faults: Sequence[Fault] = ()) -> None:
        self._lock = threading.Lock()
        self._faults: list[Fault] = list(faults)

    def add(self, *faults: Fault) -> None:
        with self._lock:
            self._faults.extend(faults)

    def clear(self) -> None:
        with self._lock:
            self._faults.clear()

    def pending(self) -> list[Fault]:
        with self._lock:
            return list(self._faults)

    def to_json(self) -> list[dict[str, object]]:
        return [fault.to_json() for fault in self.pending()]

    def take_throttle(self, op: GraphOperation) -> Throttle | None:
        return cast(Throttle | None, self._take(Throttle, lambda f: cast(Throttle, f).op == op))

    def take_network_failure(self, op: GraphOperation) -> bool:
        return self._take(NetworkFailure, lambda f: cast(NetworkFailure, f).op == op) is not None

    def take_expired_token(self) -> bool:
        return self._take(ExpireToken, lambda _f: True) is not None

    def take_gone_delta(self, root: str) -> bool:
        def _match(fault: Fault) -> bool:
            target = cast(GoneDelta, fault).root
            return target is None or target == root

        return self._take(GoneDelta, _match) is not None

    def take_not_found(self, item: str) -> bool:
        return self._take(NotFound, lambda f: cast(NotFound, f).item == item) is not None

    def take_move_conflict(self, name: str) -> bool:
        folded = name.casefold()
        return (
            self._take(ConflictOnMove, lambda f: cast(ConflictOnMove, f).name.casefold() == folded)
            is not None
        )

    def take_etag_change(self, item: str) -> bool:
        return self._take(EtagChanged, lambda f: cast(EtagChanged, f).item == item) is not None

    def _take(self, kind: type[Fault], match: Matcher) -> Fault | None:
        with self._lock:
            for fault in self._faults:
                if isinstance(fault, kind) and match(fault):
                    if fault.times is not None:
                        fault.times -= 1
                        if fault.times == 0:
                            self._faults.remove(fault)
                    return fault
        return None
