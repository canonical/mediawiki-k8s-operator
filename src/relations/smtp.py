# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Provides the Smtp class to handle smtp relations and state."""

import logging

from charms.smtp_integrator.v0.smtp import SmtpRelationData, SmtpRequires
from ops import CharmBase, Object, ObjectEvents

from exceptions import MediaWikiBlockedStatusException, MediaWikiWaitingStatusException

logger = logging.getLogger(__name__)


class Smtp(Object):
    """The Smtp relation handler."""

    def __init__(self, charm: CharmBase, relation_name: str):
        """Initialize the handler and register event handlers.

        Args:
            charm: The charm instance.
            relation_name: The name of the smtp-requires relation.
        """
        super().__init__(charm, "smtp-observer")

        self._requires = SmtpRequires(charm, relation_name)

    @property
    def on(self) -> ObjectEvents:
        """Expose the SMTP requirer's event surface."""
        return self._requires.on

    @property
    def relation_name(self) -> str:
        """Return the SMTP relation name."""
        return self._requires.relation_name

    def get_relation_data(self) -> SmtpRelationData:
        """Get the smtp relation data.

        Returns:
            SmtpRelationData: The smtp relation data.

        Raises:
            MediaWikiBlockedStatusException: If there is an error fetching the relation data.
            MediaWikiWaitingStatusException: If the relation data is not yet available.
        """
        try:
            data = self._requires.get_relation_data()
        except Exception as e:
            logger.warning("Failed to fetch smtp relation data: %s", e)
            raise MediaWikiBlockedStatusException(
                f"Error fetching {self.relation_name} relation data."
            ) from e

        if data is None:
            raise MediaWikiWaitingStatusException(
                f"Waiting for {self.relation_name} relation data."
            )

        return data

    def has_relation(self) -> bool:
        """Check if the relation exists."""
        return self.model.get_relation(self.relation_name) is not None
