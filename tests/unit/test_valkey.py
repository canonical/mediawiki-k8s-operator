# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for the Valkey client relation handler."""

from unittest.mock import Mock

import pytest

from valkey import Valkey, ValkeyConnectionInfo


def _handler() -> tuple[Valkey, Mock]:
    """Build a Valkey handler with a mocked data-interface handler."""
    handler = Valkey.__new__(Valkey)
    relation_handler = Mock()
    handler._handler = relation_handler
    return handler, relation_handler


def _response(**kwargs: object) -> Mock:
    """Build a response with valid default connection data."""
    response = Mock()
    response.endpoints = kwargs.get("endpoints", "valkey-primary:6380")
    response.username = kwargs.get("username", "relation-user")
    response.password = kwargs.get("password", "relation-password")
    response.tls = kwargs.get("tls", True)
    return response


def test_connection_info_is_normalized() -> None:
    """The first primary endpoint and all connection credentials are returned."""
    valkey, relation_handler = _handler()
    relation_handler.relations = [Mock()]
    valkey._get_response = Mock(return_value=_response())

    assert valkey.get_connection_info() == ValkeyConnectionInfo(
        host="valkey-primary",
        port=6380,
        username="relation-user",
        password="relation-password",  # nosec: B106
        tls=True,
    )


def test_connection_info_uses_first_endpoint() -> None:
    """Multiple provider endpoints use the primary endpoint for writes."""
    valkey, relation_handler = _handler()
    relation_handler.relations = [Mock()]
    valkey._get_response = Mock(
        return_value=_response(endpoints="valkey-primary:6380,valkey-replica:6380")
    )

    connection = valkey.get_connection_info()

    assert connection is not None
    assert connection.host == "valkey-primary"
    assert connection.port == 6380


@pytest.mark.parametrize(
    "response",
    [
        _response(endpoints=""),
        _response(endpoints="valkey-primary"),
        _response(username=""),
        _response(password=""),  # nosec: B106
    ],
)
def test_incomplete_response_is_unavailable(response: Mock) -> None:
    """Incomplete relation data must not produce usable connection settings."""
    valkey, relation_handler = _handler()
    relation_handler.relations = [Mock()]
    valkey._get_response = Mock(return_value=response)

    assert valkey.get_connection_info() is None


def test_no_relation_is_unavailable() -> None:
    """A missing Valkey relation has no connection information."""
    valkey, relation_handler = _handler()
    relation_handler.relations = []

    assert valkey.is_relation_available() is False
    assert valkey.get_connection_info() is None


def test_string_tls_flag_is_supported() -> None:
    """The v0-compatible string representation of the TLS flag is accepted."""
    valkey, relation_handler = _handler()
    relation_handler.relations = [Mock()]
    valkey._get_response = Mock(return_value=_response(tls="true"))

    connection = valkey.get_connection_info()

    assert connection is not None
    assert connection.tls is True
