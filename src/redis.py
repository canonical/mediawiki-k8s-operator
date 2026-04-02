# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Provides the Redis class to handle Redis relation and state."""

import logging

from charms.redis_k8s.v0.redis import RedisRequires
from ops import CharmBase, Object

logger = logging.getLogger(__name__)


class Redis(Object):
    """The Redis relation handler."""

    def __init__(self, charm: CharmBase, relation_name: str):
        """Initialize the handler and register event handlers.

        Args:
            charm: The charm instance.
            relation_name: The name of the Redis relation.
        """
        super().__init__(charm, "redis-observer")

        self.redis = RedisRequires(
            charm=charm,
            relation_name=relation_name,
        )

    def get_endpoint(self) -> str | None:
        """Get the Redis endpoint from the relation data.

        Returns:
            str: The Redis endpoint in the format "host:port". Returns None if the relation data is not available or incomplete.

        """
        if not self.is_relation_available():
            return None

        if not (relation_data := self.redis.relation_data):
            logger.warning("Redis relation data is not available.")
            return None

        hostname = relation_data.get("hostname")

        if app_data := self.redis.app_data:
            hostname = app_data.get("leader-host", hostname)

        if not hostname:
            logger.warning("Redis hostname is not available.")
            return None

        if not (port := relation_data.get("port")):
            logger.warning("Redis port is not available.")
            return None

        return f"{hostname}:{port}"

    def is_relation_available(self) -> bool:
        """Check if the relation exists."""
        return self.model.get_relation(self.redis.relation_name) is not None
