# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Manage MediaWiki peer relation data and replica secrets."""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from ops import EventBase, MaintenanceStatus, Object, Relation, RelationData, SecretNotFoundError

from exceptions import MediaWikiWaitingStatusException
from mediawiki._secrets import MediaWikiSecrets
from state import StatefulCharmBase

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MediaWikiPeerState:
    """Peer state required for one MediaWiki reconciliation cycle."""

    secrets: MediaWikiSecrets
    ro_database: bool
    force_reconciliation: bool
    composer_lock: str | None
    composer_json: str | None


class MediaWikiPeers(Object):
    """Manage distributed MediaWiki coordination through the peer relation."""

    RELATION_NAME = "mediawiki-replica"
    SECRET_LABEL = "replica-secret"  # nosec: B105
    RO_DATABASE_FLAG = "ro_db"
    FORCE_RECONCILIATION_FLAG = "force_reconciliation"
    COMPOSER_JSON_KEY = "composer_json"
    COMPOSER_LOCK_KEY = "composer_lock"

    def __init__(
        self,
        charm: StatefulCharmBase,
        relation_name: str = RELATION_NAME,
        secret_label: str = SECRET_LABEL,
    ):
        """Initialize the MediaWiki peer coordinator.

        Args:
            charm: The parent charm.
            relation_name: The MediaWiki peer relation endpoint name.
            secret_label: The label of the application secret shared by replicas.
        """
        super().__init__(charm, "mediawiki-peers")
        self._charm = charm
        self._relation_name = relation_name
        self._secret_label = secret_label

    def reconciliation_state(self) -> MediaWikiPeerState:
        """Return the peer state required for workload reconciliation."""
        relation = self._relation()
        app_data = relation.data[self._charm.app]
        return MediaWikiPeerState(
            secrets=self._replica_secrets(),
            ro_database=app_data.get(self.RO_DATABASE_FLAG, "false").lower() == "true",
            force_reconciliation=self._force_reconciliation_requested(relation.data),
            composer_lock=app_data.get(self.COMPOSER_LOCK_KEY),
            composer_json=app_data.get(self.COMPOSER_JSON_KEY),
        )

    def publish_composer_state(self, lock: str, composer_json: str) -> None:
        """Publish the leader-generated Composer state to peer application data."""
        app_data = self._relation().data[self._charm.app]
        app_data[self.COMPOSER_JSON_KEY] = composer_json
        app_data[self.COMPOSER_LOCK_KEY] = lock

    def acknowledge_database_mode(self, *, read_only: bool) -> None:
        """Publish this unit's current database mode acknowledgement."""
        self._relation().data[self._charm.unit][self.RO_DATABASE_FLAG] = str(read_only).lower()

    def reconcile_database(self, update_database_schema: Callable[[], None]) -> None:
        """Run a requested schema update after all peer units acknowledge read-only mode."""
        if not self._charm.unit.is_leader():
            return

        relation = self._relation()
        if relation.data[self._charm.app].get(self.RO_DATABASE_FLAG, "false").lower() != "true":
            return
        for unit in relation.units:
            if relation.data[unit].get(self.RO_DATABASE_FLAG, "false").lower() != "true":
                raise MediaWikiWaitingStatusException(
                    f"Waiting for unit {unit.name} to acknowledge database update by setting ro_db to true"
                )

        original_status = self._charm.unit.status
        self._charm.unit.status = MaintenanceStatus("Updating database schema")
        logger.info(
            "All units have acknowledged the database update, proceeding with database schema update"
        )
        update_database_schema()
        relation.data[self._charm.app][self.RO_DATABASE_FLAG] = "false"
        self._charm.unit.status = original_status
        logger.info("Database schema update complete")

    def setup_replica_data(self, event: EventBase) -> None:
        """Create the application secret used by MediaWiki replicas when needed."""
        if self._replica_secret_exists() or not self._charm.unit.is_leader():
            return

        logger.info("Creating replica data due to event %s", event)
        self._charm.app.add_secret(
            MediaWikiSecrets.generate().to_juju_secret(), label=self._secret_label
        )

    def rotate_secrets(self) -> None:
        """Rotate the application secret shared by MediaWiki replicas."""
        self._charm.model.get_secret(label=self._secret_label).set_content(
            MediaWikiSecrets.generate().to_juju_secret()
        )

    def request_database_update(self) -> bool:
        """Request a coordinated database schema update.

        Returns:
            Whether the peer relation was ready and the request was recorded.
        """
        relation = self._charm.model.get_relation(self._relation_name)
        if relation is None:
            return False
        relation.data[self._charm.app][self.RO_DATABASE_FLAG] = "true"
        return True

    def request_force_reconciliation(self) -> bool:
        """Request forced reconciliation on all units.

        Returns:
            Whether the peer relation was ready and the request was recorded.
        """
        relation = self._charm.model.get_relation(self._relation_name)
        if relation is None:
            return False
        relation.data[self._charm.app][self.FORCE_RECONCILIATION_FLAG] = "true"
        return True

    def _relation(self) -> Relation:
        """Return the MediaWiki peer relation."""
        relation = self._charm.model.get_relation(self._relation_name)
        if relation is None:
            raise MediaWikiWaitingStatusException(
                f"Waiting for peer relation {self._relation_name} to be ready"
            )
        return relation

    def _replica_secret_exists(self) -> bool:
        """Return whether the application replica secret exists."""
        try:
            self._charm.model.get_secret(label=self._secret_label)
        except SecretNotFoundError:
            return False
        return True

    def _replica_secrets(self) -> MediaWikiSecrets:
        """Return replica secrets, adding fields introduced by upgrades."""
        try:
            secret = self._charm.model.get_secret(label=self._secret_label)
            secrets_content = secret.get_content(refresh=True)
            expected = MediaWikiSecrets.generate().to_juju_secret()
            missing_keys = expected.keys() - secrets_content.keys()
            if missing_keys:
                if not self._charm.unit.is_leader():
                    raise MediaWikiWaitingStatusException(
                        "Waiting for leader to migrate replica secrets"
                    )
                secrets_content = dict(secrets_content)
                for key in missing_keys:
                    secrets_content[key] = expected[key]
                secret.set_content(secrets_content)
            return MediaWikiSecrets.from_juju_secret(secrets_content)
        except SecretNotFoundError:
            raise MediaWikiWaitingStatusException("Waiting for replica secrets to be available")

    def _force_reconciliation_requested(self, replica_data: RelationData) -> bool:
        """Acknowledge and return the peer force-reconciliation request."""
        app_flag = (
            replica_data[self._charm.app].get(self.FORCE_RECONCILIATION_FLAG, "false").lower()
            == "true"
        )
        if not app_flag:
            if (
                replica_data[self._charm.unit].get(self.FORCE_RECONCILIATION_FLAG, "false").lower()
                == "true"
            ):
                replica_data[self._charm.unit][self.FORCE_RECONCILIATION_FLAG] = "false"
            return False

        replica_data[self._charm.unit][self.FORCE_RECONCILIATION_FLAG] = "true"
        if self._charm.unit.is_leader() and all(
            replica_data[unit].get(self.FORCE_RECONCILIATION_FLAG, "false").lower() == "true"
            for unit in self._relation().units
        ):
            replica_data[self._charm.app][self.FORCE_RECONCILIATION_FLAG] = "false"
            logger.info("All units acknowledged force reconciliation, app flag cleared")
        return True
