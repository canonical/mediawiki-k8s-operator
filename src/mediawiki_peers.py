# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Manage MediaWiki peer relation data and replica secrets."""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from ops import (
    EventBase,
    MaintenanceStatus,
    Object,
    Relation,
    RelationDataContent,
    SecretNotFoundError,
)

from exceptions import (
    MediaWikiModeConflictError,
    MediaWikiUnknownOperationModeError,
    MediaWikiWaitingStatusException,
)
from log_redaction import register_secrets
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
            operation_mode=self._operation_mode(app_data),
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
        mode = self._operation_mode(app_data)
        return (
            mode is OperationMode.MAINTENANCE,
            app_data.get(self.MAINTENANCE_MESSAGE_KEY),
        )

    def reconcile_operation_mode(
        self, mode: OperationMode, update_database_schema: Callable[[], None]
    ) -> None:
        """Complete the requested operation once all peer units have applied it.

        Completing an operation only returns the application to normal operation. Each unit,
        including the leader, applies and acknowledges normal operation in its next
        reconciliation.

        Args:
            mode: The operation mode this unit applied during reconciliation.
            update_database_schema: Run the database schema update.

        Raises:
            MediaWikiWaitingStatusException: If a peer unit has not applied the mode yet.
        """
        if not self._charm.unit.is_leader() or mode not in (
            OperationMode.DATABASE_UPDATE,
            OperationMode.FORCE_RECONCILIATION,
        ):
            return

        relation = self._relation()
        for unit in relation.units:
            if relation.data[unit].get(self.OPERATION_MODE_KEY) != mode.value:
                raise MediaWikiWaitingStatusException(
                    f"Waiting for unit {unit.name} to apply operation mode {mode.value}"
                )

        if mode is OperationMode.DATABASE_UPDATE:
            original_status = self._charm.unit.status
            self._charm.unit.status = MaintenanceStatus("Updating database schema")
            logger.info(
                "All units have acknowledged the database update, "
                "proceeding with database schema update"
            )
            update_database_schema()
            self._charm.unit.status = original_status
            logger.info("Database schema update complete")
        elif mode is OperationMode.FORCE_RECONCILIATION:
            logger.info("All units completed forced reconciliation")

        relation.data[self._charm.app][self.OPERATION_MODE_KEY] = OperationMode.NORMAL.value

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
        current_mode = self._operation_mode(app_data)
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

    def request_maintenance_mode(
        self,
        *,
        enabled: bool,
        set_database_read_only: Callable[[bool], None],
        message: str | None = None,
    ) -> bool:
        """Request application-wide maintenance mode.

        Args:
            enabled: Whether maintenance mode should be enabled.
            set_database_read_only: Apply and verify database protection before publishing intent.
            message: Optional reason shown to MediaWiki users.

        Returns:
            Whether the peer relation was ready and the request was recorded.

        An unrecognised current mode is overridden, so operators can recover from peer data
        written by another charm revision.

        Raises:
            MediaWikiModeConflictError: If a database update or forced reconciliation is pending.
        """
        relation = self._charm.model.get_relation(self._relation_name)
        if relation is None:
            return False
        app_data = relation.data[self._charm.app]
        current_mode: OperationMode | None
        try:
            current_mode = self._operation_mode(app_data)
        except MediaWikiUnknownOperationModeError as e:
            logger.warning("Overriding unrecognised operation mode: %s", e)
            current_mode = None
        action = "enabling" if enabled else "disabling"
        if current_mode is OperationMode.DATABASE_UPDATE:
            raise MediaWikiModeConflictError(
                f"Wait for the database update to complete before {action} maintenance mode"
            )
        if current_mode is OperationMode.FORCE_RECONCILIATION:
            raise MediaWikiModeConflictError(
                f"Wait for forced reconciliation to complete before {action} maintenance mode"
            )

        if enabled and current_mode is OperationMode.NORMAL:
            self._ensure_units_in_normal_operation(relation)

        set_database_read_only(enabled)

        if enabled:
            app_data[self.OPERATION_MODE_KEY] = OperationMode.MAINTENANCE.value
            if message is not None:
                app_data[self.MAINTENANCE_MESSAGE_KEY] = message
            else:
                app_data.pop(self.MAINTENANCE_MESSAGE_KEY, None)
        elif current_mode in (OperationMode.MAINTENANCE, None):
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
        current_mode = self._operation_mode(app_data)
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

    def _operation_mode(self, app_data: RelationDataContent) -> OperationMode:
        """Return the operation mode requested in the application data.

        Raises:
            MediaWikiUnknownOperationModeError: If the requested mode is not recognised.
        """
        value = app_data.get(self.OPERATION_MODE_KEY, OperationMode.NORMAL.value)
        try:
            return OperationMode(value)
        except ValueError:
            raise MediaWikiUnknownOperationModeError(
                f"Unknown operation mode '{value}', run set-maintenance-mode to override it"
            ) from None

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
            register_secrets(*secrets_content.values())
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
