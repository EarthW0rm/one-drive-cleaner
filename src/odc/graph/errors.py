# SPDX-License-Identifier: GPL-3.0-only
"""Exceções do cliente HTTP da Graph.

As mensagens carregam apenas status, código de erro da Graph e contagens: nunca URL (pode ser
pré-autenticada), caminho, nome de arquivo, token nem texto livre vindo da resposta.
"""


class GraphError(Exception):
    """Base das falhas do cliente da Graph."""


class GraphClientError(GraphError):
    """Resposta 4xx (exceto 401/429) ou resposta inválida: não é repetida."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(f"Graph respondeu {status} ({code})")
        self.status = status
        self.code = code


class GraphNotFound(GraphClientError):  # noqa: N818 - nome definido no contrato da TF-147-02
    """404: o item ou a pasta não existe (mais)."""


class GraphConflict(GraphClientError):  # noqa: N818 - nome definido no contrato da TF-147-02
    """409: conflito de nome ou de estado."""


class GraphGone(GraphClientError):  # noqa: N818 - nome definido no contrato da TF-147-02
    """410: o token de delta expirou (resyncRequired) ou o recurso foi removido."""


class GraphPreconditionFailed(GraphClientError):  # noqa: N818 - nome definido no contrato
    """412: `If-Match` não confere com o eTag atual."""


class GraphUnavailable(GraphError):  # noqa: N818 - nome definido no contrato da TF-147-02
    """Graph indisponível: tentativas esgotadas (429/503/504, rede) ou 5xx sem retry.

    `status` é o último status HTTP recebido (`None` quando só houve falha de rede) e
    `attempts` o total de tentativas realizadas.
    """

    def __init__(self, *, status: int | None, attempts: int) -> None:
        detail = "falha de rede" if status is None else f"status {status}"
        super().__init__(f"Graph indisponível após {attempts} tentativa(s) ({detail})")
        self.status = status
        self.attempts = attempts


class GraphUnsafeUrl(GraphError):  # noqa: N818 - nome alinhado às demais exceções do módulo
    """URL absoluta fora da origem da Graph (nextLink/deltaLink adulterado) ou malformada."""

    def __init__(self) -> None:
        super().__init__("URL recusada: fora da origem da Graph")
