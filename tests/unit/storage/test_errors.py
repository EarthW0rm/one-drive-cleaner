# SPDX-License-Identifier: GPL-3.0-only
from odc.storage import errors


def test_codigos_estaveis() -> None:
    assert errors.StorageError.code == "INTERNAL"
    assert errors.StorageValidationError.code == "VALIDATION_ERROR"
    assert errors.NotFoundError.code == "NOT_FOUND"
    assert errors.ConflictError.code == "CONFLICT"
    assert errors.ProtectedCategoryError.code == "PROTECTED_CATEGORY"
