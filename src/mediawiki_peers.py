# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Manage MediaWiki peer relation data and replica secrets."""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from ops import EventBase, MaintenanceStatus, Object, Relation, SecretNotFoundError

from exceptions import MediaWikiModeConflictError, MediaWikiWaitingStatusException
from mediawiki._secrets import MediaWikiSecrets
from state import StatefulCharmBase
from types_ import OperationMode

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MediaWikiPeerState:
    """Peer state required for one MediaWiki reconciliation cycle."""

    secrets: MediaWikiSecrets
    operation_mode: OperationMode
    maintenance_message: str | None
    composer_lock: str | None
    leader_state_hash: str | None

    @property
    def database_update_requested(self) -> bool:
        """Return whether a database update is requested."""
        return self.operation_mode is OperationMode.DATABASE_UPDATE

    @property
    def maintenance_enabled(self) -> bool:
        """Return whether maintenance mode is requested."""
        return self.operation_mode is OperationMode.MAINTENANCE

    @property
    def force_reconciliation(self) -> bool:
        """Return whether forced reconciliation is requested."""
        return self.operation_mode is OperationMode.FORCE_RECONCILIATION


class MediaWikiPeers(Object):
    """Manage distributed MediaWiki coordination through the peer relation."""

    RELATION_NAME = "mediawiki-replica"
    SECRET_LABEL = "replica-secret"  # nosec: B105
    OPERATION_MODE_KEY = "operation_mode"
    MAINTENANCE_MESSAGE_KEY = "maintenance_message"
    COMPOSER_LOCK_KEY = "composer_lock"
    LEADER_STATE_HASH_KEY = "leader_state_hash"
    L10N_CACHE_VERSION_KEY = "l10n_cache_version"

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
            operation_mode=OperationMode(
                app_data.get(self.OPERATION_MODE_KEY, OperationMode.NORMAL.value)
            ),
            maintenance_message=app_data.get(self.MAINTENANCE_MESSAGE_KEY),
            composer_lock=app_data.get(self.COMPOSER_LOCK_KEY),
            leader_state_hash=app_data.get(self.LEADER_STATE_HASH_KEY),
        )

    def publish_state(self, lock: str) -> None:
        """Publish the leader-generated state to peer application data."""
        app_data = self._relation().data[self._charm.app]
        app_data[self.COMPOSER_LOCK_KEY] = lock
        app_data[self.LEADER_STATE_HASH_KEY] = self._charm.load_charm_config().state_hash

    def acknowledge_operation_mode(self, mode: OperationMode) -> None:
        """Publish the operation mode applied by this unit."""
        self._relation().data[self._charm.unit][self.OPERATION_MODE_KEY] = mode.value

    def maintenance_mode(self) -> tuple[bool, str | None] | None:
        """Return the requested maintenance mode, or None if the peer relation is unavailable."""
        relation = self._charm.model.get_relation(self._relation_name)
        if relation is None:
            return None
        app_data = relation.data[self._charm.app]
        mode = OperationMode(app_data.get(self.OPERATION_MODE_KEY, OperationMode.NORMAL.value))
        return (
            mode is OperationMode.MAINTENANCE,
            app_data.get(self.MAINTENANCE_MESSAGE_KEY),
        )

    def reconcile_operation_mode(self, mode: OperationMode) -> None:
        """Wait for all peer units to apply the requested operation mode."""
        if not self._charm.unit.is_leader():
            return

        relation = self._relation()
        for unit in relation.units:
            if relation.data[unit].get(self.OPERATION_MODE_KEY) != mode.value:
                raise MediaWikiWaitingStatusException(
                    f"Waiting for unit {unit.name} to apply operation mode {mode.value}"
                )
        if mode is OperationMode.FORCE_RECONCILIATION:
            relation.data[self._charm.app][self.OPERATION_MODE_KEY] = OperationMode.NORMAL.value
            logger.info("All units completed forced reconciliation")

    def localisation_cache_version(self) -> str | None:
        """Return the MediaWiki version of this unit's last successful localisation cache rebuild.

        Returns:
            The MediaWiki version string, or None if no rebuild has been recorded.
        """
        return self._relation().data[self._charm.unit].get(self.L10N_CACHE_VERSION_KEY)

    def mark_localisation_cache_rebuilt(self, version: str) -> None:
        """Record the MediaWiki version of this unit's last successful localisation cache rebuild.

        Args:
            version: The MediaWiki version the localisation cache was rebuilt against.
        """
        self._relation().data[self._charm.unit][self.L10N_CACHE_VERSION_KEY] = version

    def reconcile_database_update(self, update_database_schema: Callable[[], None]) -> None:
        """Run a requested schema update after all peer units acknowledge read-only mode."""
        if not self._charm.unit.is_leader():
            return

        relation = self._relation()
        app_data = relation.data[self._charm.app]
        if (
            app_data.get(self.OPERATION_MODE_KEY, OperationMode.NORMAL.value)
            != OperationMode.DATABASE_UPDATE.value
        ):
            return

        original_status = self._charm.unit.status
        self._charm.unit.status = MaintenanceStatus("Updating database schema")
        logger.info(
            "All units have acknowledged the database update, proceeding with database schema update"
        )
        update_database_schema()
        app_data[self.OPERATION_MODE_KEY] = OperationMode.NORMAL.value
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
        app_data = relation.data[self._charm.app]
        current_mode = OperationMode(
            app_data.get(self.OPERATION_MODE_KEY, OperationMode.NORMAL.value)
        )
        if current_mode is OperationMode.MAINTENANCE:
            raise MediaWikiModeConflictError(
                "Disable maintenance mode before requesting a database update"
            )
        if current_mode is OperationMode.FORCE_RECONCILIATION:
            raise MediaWikiModeConflictError(
                "Wait for forced reconciliation to complete before requesting a database update"
            )
        self._ensure_units_in_normal_operation(relation)
        app_data[self.OPERATION_MODE_KEY] = OperationMode.DATABASE_UPDATE.value
        return True

    def request_maintenance_mode(self, *, enabled: bool, message: str | None = None) -> bool:
        """Request application-wide maintenance mode.

        Args:
            enabled: Whether maintenance mode should be enabled.
            message: Optional reason shown to MediaWiki users.

        Returns:
            Whether the peer relation was ready and the request was recorded.

        Raises:
            MediaWikiModeConflictError: If a database update is pending.
        """
        relation = self._charm.model.get_relation(self._relation_name)
        if relation is None:
            return False
        app_data = relation.data[self._charm.app]
        current_mode = OperationMode(
            app_data.get(self.OPERATION_MODE_KEY, OperationMode.NORMAL.value)
        )
        if enabled and current_mode is OperationMode.DATABASE_UPDATE:
            raise MediaWikiModeConflictError(
                "Wait for the database update to complete before enabling maintenance mode"
            )
        if enabled and current_mode is OperationMode.FORCE_RECONCILIATION:
            raise MediaWikiModeConflictError(
                "Wait for forced reconciliation to complete before enabling maintenance mode"
            )

        if enabled:
            if current_mode is OperationMode.NORMAL:
                self._ensure_units_in_normal_operation(relation)
            app_data[self.OPERATION_MODE_KEY] = OperationMode.MAINTENANCE.value
            if message is not None:
                app_data[self.MAINTENANCE_MESSAGE_KEY] = message
            else:
                app_data.pop(self.MAINTENANCE_MESSAGE_KEY, None)
        elif current_mode is OperationMode.MAINTENANCE:
            app_data[self.OPERATION_MODE_KEY] = OperationMode.NORMAL.value
            app_data.pop(self.MAINTENANCE_MESSAGE_KEY, None)
        return True

    def request_force_reconciliation(self) -> bool:
        """Request forced reconciliation on all units.

        Returns:
            Whether the peer relation was ready and the request was recorded.
        """
        relation = self._charm.model.get_relation(self._relation_name)
        if relation is None:
            return False
        app_data = relation.data[self._charm.app]
        current_mode = OperationMode(
            app_data.get(self.OPERATION_MODE_KEY, OperationMode.NORMAL.value)
        )
        if current_mode is not OperationMode.NORMAL:
            raise MediaWikiModeConflictError(
                f"Wait for {current_mode.value} to complete before forcing reconciliation"
            )
        self._ensure_units_in_normal_operation(relation)
        app_data[self.OPERATION_MODE_KEY] = OperationMode.FORCE_RECONCILIATION.value
        return True

    def _ensure_units_in_normal_operation(self, relation: Relation) -> None:
        """Ensure no unit still reports a previously completed operation."""
        units = (self._charm.unit, *relation.units)
        if any(
            relation.data[unit].get(self.OPERATION_MODE_KEY, OperationMode.NORMAL.value)
            != OperationMode.NORMAL.value
            for unit in units
        ):
            raise MediaWikiModeConflictError(
                "Wait for all units to return to normal operation before starting another operation"
            )

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
