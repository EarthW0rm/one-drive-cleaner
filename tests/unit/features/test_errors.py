# SPDX-License-Identifier: GPL-3.0-only
import pytest

from odc.features.errors import (
    EmbedderConfigError,
    EmbedderError,
    EmbeddingError,
    WeightsUnavailableError,
)


@pytest.mark.parametrize("error", [EmbedderConfigError, WeightsUnavailableError, EmbeddingError])
def test_erros_do_embedder_herdam_da_base_comum(error: type[EmbedderError]) -> None:
    with pytest.raises(EmbedderError, match="falhou"):
        raise error("falhou")
