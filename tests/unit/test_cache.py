# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for key-value store relation selection."""

from unittest.mock import Mock

import pytest

from cache import Cache, CacheConnectionInfo
from exceptions import MediaWikiBlockedStatusException
from valkey import ValkeyConnectionInfo


def test_valkey_is_selected_over_unavailable_redis() -> None:
    """Valkey connection information is normalized for MediaWiki consumers."""
    redis = Mock()
    redis.is_relation_available.return_value = False
    valkey = Mock()
    valkey.is_relation_available.return_value = True
    valkey.get_connection_info.return_value = ValkeyConnectionInfo(
        host="valkey-0",
        port=6380,
        username="user",
        password="password",
        tls=True,  # nosec: B106
    )

    cache = Cache.__new__(Cache)
    cache._redis = redis
    cache._valkey = valkey

    assert cache.get_connection_info() == CacheConnectionInfo(
        endpoint="valkey-0:6380",
        username="user",
        password="password",  # nosec: B106
        tls=True,
    )


def test_redis_is_selected_when_valkey_is_unavailable() -> None:
    """The existing Redis relation remains a supported backend."""
    redis = Mock()
    redis.is_relation_available.return_value = True
    redis.get_endpoint.return_value = "redis-host:6379"
    valkey = Mock()
    valkey.is_relation_available.return_value = False

    cache = Cache.__new__(Cache)
    cache._redis = redis
    cache._valkey = valkey

    assert cache.get_connection_info() == CacheConnectionInfo(endpoint="redis-host:6379")


def test_both_relations_are_rejected() -> None:
    """The mutually exclusive backends cannot be used together."""
    redis = Mock()
    redis.is_relation_available.return_value = True
    valkey = Mock()
    valkey.is_relation_available.return_value = True

    cache = Cache.__new__(Cache)
    cache._redis = redis
    cache._valkey = valkey

    with pytest.raises(MediaWikiBlockedStatusException, match="mutually exclusive"):
        cache.validate()


def test_missing_valkey_connection_data_is_unavailable() -> None:
    """A related Valkey without a complete response is not usable."""
    redis = Mock()
    redis.is_relation_available.return_value = False
    valkey = Mock()
    valkey.is_relation_available.return_value = True
    valkey.get_connection_info.return_value = None

    cache = Cache.__new__(Cache)
    cache._redis = redis
    cache._valkey = valkey

    assert cache.get_connection_info() is None
