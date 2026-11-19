# SPDX-License-Identifier: GPL-3.0-only
"""Pontos de parada determinísticos do Graph fake (resiliência, TF-157-03).

O teste registra "segure a n-ésima requisição do tipo X nesta fase" e espera o servidor avisar que
chegou lá (`Breakpoint.wait_reached`): nenhum `sleep` decide o instante. Com a requisição retida, o
teste faz o que quiser (tipicamente matar o `odc` com SIGKILL) e então a libera (`release`) ou a
derruba (`drop`: o servidor encerra a conexão sem responder).

Chaves: o valor de `GraphOperation` para a API (`move`, `delta`, ...) e `cdn_thumb`/`cdn_content`
para o CDN. Fases: `BEFORE` (a requisição chegou; nada foi aplicado), `AFTER` (efeito aplicado no
drive; resposta ainda não enviada) e `MID_BODY` (só o download do CDN: metade do corpo já foi
enviada). A contagem `nth` começa no instante do registro e cada ponto dispara uma única vez.
"""

import asyncio
import threading
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

HOLD_TIMEOUT_S: Final = 120.0
"""Rede de segurança: uma requisição retida e esquecida não bloqueia o servidor para sempre."""
CDN_THUMB: Final = "cdn_thumb"
CDN_CONTENT: Final = "cdn_content"


class Phase(StrEnum):
    BEFORE = "before"
    AFTER = "after"
    MID_BODY = "mid_body"


@dataclass(eq=False)
class Breakpoint:
    """Uma requisição retida (ou a reter). Usado pela thread do teste."""

    key: str
    nth: int
    phase: Phase
    base: int = 0
    reached: threading.Event = field(default_factory=threading.Event)
    _go: threading.Event = field(default_factory=threading.Event)
    dropped: bool = False

    def wait_reached(self, timeout: float = HOLD_TIMEOUT_S) -> bool:
        """Bloqueia até o servidor reter a requisição (ou o prazo); sem polling por tempo."""
        return self.reached.wait(timeout)

    def release(self) -> None:
        """Deixa a requisição seguir normalmente."""
        self._go.set()

    def drop(self) -> None:
        """Derruba a requisição: o servidor encerra a conexão sem aplicar nem responder."""
        self.dropped = True
        self._go.set()

    def hold(self, timeout: float = HOLD_TIMEOUT_S) -> None:
        """Lado do servidor: sinaliza a chegada e aguarda `release`/`drop`."""
        self.reached.set()
        self._go.wait(timeout)


class Breakpoints:
    """Registro de pontos de parada, seguro entre a thread do teste e a do servidor."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}
        self._points: list[Breakpoint] = []

    def add(self, key: str, nth: int, phase: Phase = Phase.BEFORE) -> Breakpoint:
        """Retém a `nth`-ésima requisição de `key` (contada a partir de agora) na `phase`."""
        if nth < 1:
            message = "nth deve ser >= 1"
            raise ValueError(message)
        with self._lock:
            point = Breakpoint(key, nth, phase, base=self._counts.get(key, 0))
            self._points.append(point)
        return point

    def arrive(self, key: str) -> int:
        """Registra a chegada de uma requisição de `key`; devolve o número de ordem dela."""
        with self._lock:
            number = self._counts.get(key, 0) + 1
            self._counts[key] = number
            return number

    def find(self, key: str, number: int, phase: Phase) -> Breakpoint | None:
        """Ponto que retém a requisição `number` de `key` nesta fase (ou `None`)."""
        with self._lock:
            for point in self._points:
                if (point.key, point.phase) == (key, phase) and point.base + point.nth == number:
                    return point
        return None

    async def pause(self, key: str, number: int, phase: Phase) -> Breakpoint | None:
        """Retém a requisição se houver ponto para ela; devolve o ponto (ou `None`)."""
        point = self.find(key, number, phase)
        if point is not None:
            await asyncio.to_thread(point.hold)
        return point

    def release_all(self) -> None:
        """Libera tudo (encerramento do servidor): nenhuma thread fica presa."""
        with self._lock:
            points = list(self._points)
        for point in points:
            point.release()
