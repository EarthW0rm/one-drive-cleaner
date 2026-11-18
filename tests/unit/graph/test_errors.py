# SPDX-License-Identifier: GPL-3.0-only
import pytest

from odc.graph.errors import (
    GraphClientError,
    GraphConflict,
    GraphError,
    GraphGone,
    GraphNotFound,
    GraphPreconditionFailed,
    GraphUnavailable,
    GraphUnsafeUrl,
)


@pytest.mark.parametrize(
    "error_type", [GraphNotFound, GraphConflict, GraphGone, GraphPreconditionFailed]
)
def test_typed_client_errors_derive_from_client_error(error_type: type[GraphClientError]) -> None:
    error = error_type(404, "itemNotFound")

    assert isinstance(error, GraphClientError)
    assert isinstance(error, GraphError)
    assert (error.status, error.code) == (404, "itemNotFound")
    assert "404" in str(error)
    assert "itemNotFound" in str(error)


def test_unavailable_reports_status_and_attempts() -> None:
    error = GraphUnavailable(status=503, attempts=5)

    assert (error.status, error.attempts) == (503, 5)
    assert "503" in str(error)
    assert "5" in str(error)


def test_unavailable_without_status_means_network_failure() -> None:
    error = GraphUnavailable(status=None, attempts=5)

    assert error.status is None
    assert "rede" in str(error)


def test_unsafe_url_message_has_no_url() -> None:
    assert "http" not in str(GraphUnsafeUrl())
