# SPDX-License-Identifier: GPL-3.0-only
import pytest

from odc.ml.errors import InsufficientLabels, InvalidModelError


def test_erros_sao_distintos_e_carregam_a_mensagem() -> None:
    assert not issubclass(InsufficientLabels, InvalidModelError)
    assert not issubclass(InvalidModelError, InsufficientLabels)
    with pytest.raises(InsufficientLabels, match="poucos"):
        raise InsufficientLabels("poucos rótulos")
    with pytest.raises(InvalidModelError, match="adulterado"):
        raise InvalidModelError("adulterado")
