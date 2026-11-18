# SPDX-License-Identifier: GPL-3.0-only
import pytest

from odc.features.errors import (
    EmbedderConfigError,
    EmbedderError,
    EmbeddingError,
    TempCapReachedError,
    WeightsUnavailableError,
)


@pytest.mark.parametrize("error", [EmbedderConfigError, WeightsUnavailableError, EmbeddingError])
def test_erros_do_embedder_herdam_da_base_comum(error: type[EmbedderError]) -> None:
    with pytest.raises(EmbedderError, match="falhou"):
        raise error("falhou")


def test_teto_do_temporario_nao_e_um_erro_do_embedder() -> None:
    assert not issubclass(TempCapReachedError, EmbedderError)
    with pytest.raises(TempCapReachedError):
        raise TempCapReachedError
