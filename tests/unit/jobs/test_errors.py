# SPDX-License-Identifier: GPL-3.0-only
import pytest

from odc.jobs.errors import (
    INTERNAL,
    FatalJobError,
    JobInterrupted,
    PauseError,
    RetryableError,
    checked_error_code,
)
from odc.jobs.types import PauseReason


def test_retryable_error_usa_internal_por_padrao_e_aceita_codigo() -> None:
    assert RetryableError().error_code == INTERNAL
    assert RetryableError("UPSTREAM_DOWN").error_code == "UPSTREAM_DOWN"


def test_pause_error_aceita_texto_ou_enum() -> None:
    assert PauseError("network").reason is PauseReason.NETWORK
    assert PauseError(PauseReason.TEMP_CAP).reason is PauseReason.TEMP_CAP
    assert "network" in str(PauseError("network"))


def test_pause_error_rejeita_motivo_desconhecido() -> None:
    with pytest.raises(ValueError, match="offline"):
        PauseError("offline")


def test_fatal_job_error_guarda_codigo_exibivel() -> None:
    error = FatalJobError("MODEL_MISMATCH")

    assert error.error_code == "MODEL_MISMATCH"
    assert str(error) == "falha definitiva (MODEL_MISMATCH)"


@pytest.mark.parametrize("code", ["", "minusculo", "1COMECA_COM_DIGITO", "COM ESPACO", "A" * 65])
def test_codigo_fora_do_formato_e_rejeitado(code: str) -> None:
    with pytest.raises(ValueError, match="error_code inválido"):
        checked_error_code(code)
    with pytest.raises(ValueError, match="error_code inválido"):
        FatalJobError(code)


def test_job_interrupted_e_um_sinal_de_controle() -> None:
    assert str(JobInterrupted()) == "job interrompido a pedido"
