# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Select and normalize the MediaWiki key-value store relation."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ops import CharmBase, Object

from exceptions import MediaWikiBlockedStatusException

if TYPE_CHECKING:
    from redis import Redis
    from valkey import Valkey


@dataclass(frozen=True)
class CacheConnectionInfo:
    """Normalized connection details for a MediaWiki key-value store."""

    endpoint: str
    username: str | None = None
    password: str | None = None
    tls: bool = False
    tls_ca: str | None = None


class Cache(Object):
    """Select Redis or Valkey as the MediaWiki key-value store."""

    def __init__(self, charm: CharmBase, redis: "Redis", valkey: "Valkey"):
        """Initialize the key-value store selector.

        Args:
            charm: The parent charm.
            redis: The legacy Redis relation handler.
            valkey: The Valkey relation handler.
        """
        super().__init__(charm, "cache-selector")
        self._redis = redis
        self._valkey = valkey

    def validate(self) -> None:
        """Raise if both mutually exclusive key-value store relations exist."""
        if self._redis.is_relation_available() and self._valkey.is_relation_available():
            raise MediaWikiBlockedStatusException(
                "Redis and Valkey relations are mutually exclusive; remove one relation."
            )

    def get_connection_info(self) -> CacheConnectionInfo | None:
        """Return the normalized connection details for the active store."""
        self.validate()

        if self._valkey.is_relation_available():
            connection = self._valkey.get_connection_info()
            if connection is None:
                return None
            return CacheConnectionInfo(
                endpoint=f"{connection.host}:{connection.port}",
                username=connection.username,
                password=connection.password,
                tls=connection.tls,
                tls_ca=connection.tls_ca,
            )

        if self._redis.is_relation_available():
            endpoint = self._redis.get_endpoint()
            if endpoint is not None:
                return CacheConnectionInfo(endpoint=endpoint)

        return None
