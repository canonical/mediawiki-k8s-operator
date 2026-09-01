# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Provides the Valkey client relation handler."""

import logging
from dataclasses import dataclass

from dpcharmlibs.interfaces import (
    DataContractV1,
    RequirerCommonModel,
    ResourceRequirerEventHandler,
    ValkeyResponseModel,
    build_model,
)
from ops import CharmBase, Object, ObjectEvents
from pydantic import TypeAdapter

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ValkeyConnectionInfo:
    """Connection details provided by the Valkey charm."""

    host: str
    port: int
    username: str
    password: str
    tls: bool


class Valkey(Object):
    """Handle the ``valkey_client`` relation."""

    def __init__(self, charm: CharmBase, relation_name: str):
        """Initialize the Valkey client relation.

        Args:
            charm: The parent charm.
            relation_name: The relation endpoint name.
        """
        super().__init__(charm, "valkey-observer")
        self._handler = ResourceRequirerEventHandler(
            charm=charm,
            relation_name=relation_name,
            requests=[RequirerCommonModel(resource="*")],
            response_model=ValkeyResponseModel,
        )

    @property
    def on(self) -> ObjectEvents:
        """Expose relation events from the data interface handler."""
        return self._handler.on  # type: ignore[return-value]

    def is_relation_available(self) -> bool:
        """Return whether a Valkey relation exists."""
        return bool(self._handler.relations)

    def _get_response(self) -> ValkeyResponseModel | None:
        """Return the first response from the Valkey provider."""
        if not (relations := self._handler.relations):
            return None

        relation = relations[0]
        try:
            contract: DataContractV1[ValkeyResponseModel] = build_model(
                self._handler.interface.repository(relation.id, relation.app),
                TypeAdapter(DataContractV1[ValkeyResponseModel]),  # type: ignore[arg-type]
            )
        except Exception:
            logger.warning(
                "Could not read Valkey response from relation data: relation_id=%s app=%s",
                relation.id,
                relation.app.name if relation.app else None,
                exc_info=True,
            )
            return None

        return contract.requests[0] if contract.requests else None

    def get_connection_info(self) -> ValkeyConnectionInfo | None:
        """Return complete Valkey connection information, if available."""
        response = self._get_response()
        if response is None or not response.endpoints:
            return None

        endpoint = response.endpoints.split(",", 1)[0].strip()
        if ":" not in endpoint:
            logger.warning("Valkey endpoint is missing a port: %r", endpoint)
            return None

        host, port_text = endpoint.rsplit(":", 1)
        try:
            port = int(port_text)
        except ValueError:
            logger.warning("Valkey port is not a valid integer: %r", port_text)
            return None

        if not response.username or not response.password:
            logger.warning("Valkey credentials are not available.")
            return None

        tls = (
            response.tls if isinstance(response.tls, bool) else str(response.tls).lower() == "true"
        )
        return ValkeyConnectionInfo(
            host=host,
            port=port,
            username=response.username,
            password=response.password,
            tls=tls,
        )
