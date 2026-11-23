# SPDX-License-Identifier: GPL-3.0-only
"""Erros do embedder local (TF-151-02).

`odc.features` fica abaixo de `odc.jobs` nas camadas, então estes erros não herdam dos sinais de
job: o handler de extração os traduz (`WeightsUnavailableError` -> `RetryableError`;
`EmbedderConfigError` -> `FatalJobError`). As mensagens não carregam caminhos nem dados do
utilizador.
"""


class EmbedderError(Exception):
    """Base dos erros do embedder."""


class EmbedderConfigError(EmbedderError):
    """Configuração inválida: nome do modelo, dispositivo, dimensão ou ausência de modelo ativo."""


class WeightsUnavailableError(EmbedderError):
    """Pesos não obtidos (download ou leitura do cache falhou); transitório, vale reexecutar."""


class EmbeddingError(EmbedderError):
    """A inferência falhou (inclusive após o fallback para CPU)."""


class TempCapReachedError(Exception):
    """O diretório temporário atingiu o teto (RN-03): a extração pausa até a limpeza.

    Não é um `EmbedderError`: vem da política de disco, não do embedder. O adaptador do job
    traduz para `PauseError(temp_cap)`.
    """
