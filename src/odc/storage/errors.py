# SPDX-License-Identifier: GPL-3.0-only
"""Erros estáveis da camada de dados; `code` segue o catálogo de erros da API (plan 5)."""


class StorageError(Exception):
    """Erro base da camada de dados."""

    code = "INTERNAL"


class StorageValidationError(StorageError):
    """Entrada inválida (cursor adulterado, chave desconhecida, valor fora do domínio)."""

    code = "VALIDATION_ERROR"


class NotFoundError(StorageError):
    """Registro inexistente."""

    code = "NOT_FOUND"


class ConflictError(StorageError):
    """Operação incompatível com o estado atual do registro."""

    code = "CONFLICT"


class ProtectedCategoryError(StorageError):
    """Categoria protegida não pode ser arquivada (RN-09)."""

    code = "PROTECTED_CATEGORY"
