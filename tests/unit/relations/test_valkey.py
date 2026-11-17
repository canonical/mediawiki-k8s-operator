# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for the Valkey client relation handler."""

from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from exceptions import MediaWikiBlockedStatusException, MediaWikiWaitingStatusException
from relations.valkey import Valkey, ValkeyConnectionInfo


def _handler() -> tuple[Valkey, Mock]:
    """Build a Valkey handler with a mocked data-interface handler."""
    handler = Valkey.__new__(Valkey)
    handler._relation_name = "valkey"
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
    response.tls_ca = kwargs.get("tls_ca")
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


def test_connection_info_includes_tls_ca() -> None:
    """The provider TLS CA is included with the normalized connection details."""
    valkey, relation_handler = _handler()
    relation_handler.relations = [Mock()]
    valkey._get_response = Mock(return_value=_response(tls_ca="valkey-ca"))

    connection = valkey.get_connection_info()

    assert connection is not None
    assert connection.tls_ca == "valkey-ca"


@pytest.mark.parametrize(
    "response",
    [
        None,
        _response(endpoints=""),
        _response(endpoints="valkey-primary"),
        _response(endpoints="valkey-primary:invalid"),
        _response(username=""),
        _response(password=""),  # nosec: B106
    ],
)
def test_incomplete_response_waits(response: Mock | None) -> None:
    """Incomplete relation data waits rather than enabling a database fallback."""
    valkey, relation_handler = _handler()
    relation_handler.relations = [Mock()]
    valkey._get_response = Mock(return_value=response)

    with pytest.raises(MediaWikiWaitingStatusException, match="Valkey relation is not ready"):
        valkey.get_connection_info()


def test_no_relation_blocks() -> None:
    """A missing Valkey relation blocks operation."""
    valkey, relation_handler = _handler()
    relation_handler.relations = []

    with pytest.raises(MediaWikiBlockedStatusException, match="Valkey relation is required"):
        valkey.get_connection_info()


@pytest.mark.parametrize(
    "response",
    [
        None,
        _response(endpoints=""),
        _response(username=""),
        _response(password=""),  # nosec: B106
    ],
)
def test_incomplete_relation_waits(response: Mock | None) -> None:
    """Readiness waits while the provider has not supplied connection fields."""
    valkey, relation_handler = _handler()
    relation_handler.relations = [Mock()]
    valkey._get_response = Mock(return_value=response)

    with pytest.raises(MediaWikiWaitingStatusException, match="Valkey relation is not ready"):
        valkey.ensure_ready()


def test_readiness_requires_relation() -> None:
    """A missing relation is not ready."""
    valkey, relation_handler = _handler()
    relation_handler.relations = []

    assert valkey.has_relation() is False
    with pytest.raises(MediaWikiBlockedStatusException, match="Valkey relation is required"):
        valkey.ensure_ready()


def test_readiness_trusts_provider_data() -> None:
    """Complete provider data is sufficient without reaching the provided server."""
    valkey, relation_handler = _handler()
    relation_handler.relations = [Mock()]
    valkey._get_response = Mock(return_value=_response(endpoints="unreachable.invalid:6380"))
    assert valkey.has_relation() is True
    valkey.ensure_ready()


def test_invalid_response_waits() -> None:
    """Schema-invalid provider data waits and is not considered ready."""
    valkey, relation_handler = _handler()
    relation_handler.relations = [Mock()]
    valkey._get_response = Mock(
        side_effect=ValidationError.from_exception_data("ValkeyResponseModel", [])
    )

    with pytest.raises(MediaWikiWaitingStatusException, match="Valkey relation is not ready"):
        valkey.get_connection_info()
    with pytest.raises(MediaWikiWaitingStatusException, match="Valkey relation is not ready"):
        valkey.ensure_ready()


@pytest.mark.parametrize("tls", [True, False, "true", "false"])
def test_tls_flag_is_supported(tls: bool | str) -> None:
    """Boolean and string TLS flags preserve the provider's transport choice."""
    valkey, relation_handler = _handler()
    relation_handler.relations = [Mock()]
    valkey._get_response = Mock(return_value=_response(tls=tls))

    connection = valkey.get_connection_info()

    assert connection is not None
    assert connection.tls is (tls is True or tls == "true")
