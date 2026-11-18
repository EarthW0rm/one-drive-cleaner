# SPDX-License-Identifier: GPL-3.0-only
"""Erros que um handler levanta para dizer ao runner o que fazer com o job (plan 4.3, 5.1).

- `RetryableError`: falha transitória; o runner reenfileira com backoff (5/30/120 s) e, esgotadas
  as tentativas, marca `failed(error_code)`.
- `PauseError(reason)`: o job vai para `paused(reason)` e é retomado quando a causa cessa.
- `FatalJobError(error_code)`: falha definitiva; `failed(error_code)` sem nova tentativa.
- `JobInterrupted`: o handler parou a pedido (`ctx.cancelled`) depois de salvar o checkpoint; o
  job volta à fila sem contar tentativa.

Os `error_code` ficam visíveis na interface: MAIUSCULAS_COM_UNDERSCORE, sem dado do utilizador.
"""

import re

from odc.jobs.types import PauseReason

ERROR_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

INVALID_PAYLOAD = "INVALID_PAYLOAD"
GRAPH_UNAVAILABLE = "GRAPH_UNAVAILABLE"
DISK_FULL = "DISK_FULL"
INTERNAL = "INTERNAL"


def checked_error_code(value: str) -> str:
    """Devolve `value` se estiver no formato de `error_code`; senão `ValueError`."""
    if not ERROR_CODE_PATTERN.fullmatch(value):
        raise ValueError("error_code inválido (esperado MAIUSCULAS_COM_UNDERSCORE)")
    return value


class JobError(Exception):
    """Base dos sinais de handler para o runner."""


class RetryableError(JobError):
    """Falha transitória não absorvida pelo cliente da Graph; `error_code` vale se esgotar."""

    def __init__(self, error_code: str = INTERNAL) -> None:
        self.error_code = checked_error_code(error_code)
        super().__init__(f"falha transitória ({self.error_code})")


class PauseError(JobError):
    """Pausa o job até a causa cessar (`network`, `login_required`, `temp_cap`, `user`)."""

    def __init__(self, reason: PauseReason | str) -> None:
        self.reason = PauseReason(reason)
        super().__init__(f"job pausado ({self.reason.value})")


class FatalJobError(JobError):
    """Falha definitiva: o job termina `failed(error_code)` sem nova tentativa."""

    def __init__(self, error_code: str) -> None:
        self.error_code = checked_error_code(error_code)
        super().__init__(f"falha definitiva ({self.error_code})")


class JobInterrupted(JobError):  # noqa: N818 - é um sinal de controle, não uma falha
    """O handler atendeu ao pedido de cancelamento; o job volta à fila (retomada do checkpoint)."""

    def __init__(self) -> None:
        super().__init__("job interrompido a pedido")
