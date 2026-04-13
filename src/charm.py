#!/usr/bin/env python3

# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm Operator for mediawiki-k8s."""

import logging
import typing
from urllib.parse import urlparse

import ops
from charms.redis_k8s.v0.redis import RedisRelationCharmEvents
from charms.traefik_k8s.v0.traefik_route import TraefikRouteRequirer
from ops import (
    ActionEvent,
    ActiveStatus,
    BlockedStatus,
    EventBase,
    MaintenanceStatus,
    ModelError,
    Relation,
    RelationData,
    SecretNotFoundError,
    WaitingStatus,
    pebble,
)

from database import Database
from exceptions import (
    CharmConfigInvalidError,
    MediaWikiBlockedStatusException,
    MediaWikiStatusException,
    MediaWikiWaitingStatusException,
)
from mediawiki import MediaWiki, MediaWikiSecrets
from mediawiki_api import SiteInfo
from oauth import OAuth
from redis import Redis
from s3 import S3
from state import StatefulCharmBase

# Log messages can be retrieved using juju debug-log
logger = logging.getLogger(__name__)


class Charm(StatefulCharmBase):
    """Charm for MediaWiki on Kubernetes."""

    _CONTAINER_NAME = "mediawiki"
    _SERVICE_NAME = "mediawiki"
    _REDIS_JOB_SERVICES = ("redisJobRunnerService", "redisJobChronService")

    _DATABASE_RELATION_NAME = "database"
    _DATABASE_NAME = "mediawiki"

    _INGRESS_RELATION_NAME = "traefik-route"

    _OAUTH_RELATION_NAME = "oauth"
    _REDIS_RELATION_NAME = "redis"
    _S3_RELATION_NAME = "s3-parameters"

    _PEER_RELATION_NAME = "mediawiki-replica"
    _REPLICA_SECRET_LABEL = "replica-secret"  # nosec: B105

    _RO_DATABASE_FLAG = "ro_db"

    _SSH_KEY_MEDIAWIKI_FIELD = "mediawiki"
    _SSH_KEY_GIT_SYNC_FIELD = "git-sync"
    _SSH_KEY_FIELDS = frozenset({_SSH_KEY_MEDIAWIKI_FIELD, _SSH_KEY_GIT_SYNC_FIELD})

    on = RedisRelationCharmEvents()  # pyright: ignore[reportAssignmentType,reportIncompatibleMethodOverride]

    def __init__(self, *args: typing.Any):
        super().__init__(*args)

        self._database = Database(self, self._DATABASE_RELATION_NAME, self._DATABASE_NAME)
        self._oauth = OAuth(self, self._OAUTH_RELATION_NAME)
        self._redis = Redis(self, self._REDIS_RELATION_NAME)
        self._s3 = S3(self, self._S3_RELATION_NAME)
        self._mediawiki = MediaWiki(self, self._database, self._oauth, self._redis, self._s3)

        self._ingress_requirer = TraefikRouteRequirer(
            self,
            self.model.get_relation(self._INGRESS_RELATION_NAME),  # type: ignore[arg-type]  # https://github.com/canonical/traefik-k8s-operator/issues/448
            relation_name=self._INGRESS_RELATION_NAME,
        )

        self.framework.observe(self.on.leader_elected, self._setup_replica_data)

        # Reconciliation events
        reconciliation_events = [
            self.on.mediawiki_pebble_ready,
            self._database.db.on.database_created,
            self._database.db.on.endpoints_changed,
            self.on[self._DATABASE_RELATION_NAME].relation_broken,
            self.on[self._OAUTH_RELATION_NAME].relation_created,
            self.on[self._OAUTH_RELATION_NAME].relation_changed,
            self._oauth.oauth.on.oauth_info_changed,
            self._oauth.oauth.on.oauth_info_removed,
            self.on.redis_relation_updated,
            self._s3.s3.on.credentials_changed,
            self._s3.s3.on.credentials_gone,
            self.on.traefik_route_relation_joined,
            self.on.traefik_route_relation_changed,
            self.on.traefik_route_relation_broken,
            self.on.config_changed,
            self.on.secret_changed,
            self.on[self._PEER_RELATION_NAME].relation_changed,
        ]
        for event in reconciliation_events:
            self.framework.observe(event, self._reconciliation)

        # Actions
        self.framework.observe(
            self.on.rotate_mediawiki_secrets_action, self._on_rotate_mediawiki_secrets
        )
        self.framework.observe(
            self.on.rotate_root_credentials_action, self._on_rotate_root_credentials
        )
        self.framework.observe(self.on.update_database_action, self._on_update_database)

    @property
    def _container(self) -> ops.Container:
        """Return the MediaWiki container."""
        return self.unit.get_container(self._CONTAINER_NAME)

    def _init_pebble_layer(self) -> None:
        """Initialize the Pebble layer for the MediaWiki container."""
        health_check_timeout = 5
        php_path = "/usr/bin/php"
        job_runner_service_dir = "/opt/redis-job-runner-service"
        layer: pebble.LayerDict = {
            "summary": "mediawiki layer",
            "description": "Pebble layer configuration for MediaWiki",
            "services": {
                self._SERVICE_NAME: {
                    "override": "replace",
                    "summary": "MediaWiki service (apache)",
                    "command": "/usr/sbin/apache2ctl -D FOREGROUND",
                },
                **{
                    service: {
                        "override": "replace",
                        "summary": f"MediaWiki {service}",
                        "command": f"{php_path} {job_runner_service_dir}/{service} --config-file={self._mediawiki.JOB_RUNNER_CONFIG_PATH}",
                    }
                    for service in self._REDIS_JOB_SERVICES
                },
            },
            "checks": {
                "mediawiki-api-ready": {
                    "override": "replace",
                    "level": "ready",
                    "http": {
                        "url": "http://localhost/w/api.php?action=query&format=json&prop=&meta=siteinfo&formatversion=2"
                    },
                    "period": f"{max(10, health_check_timeout * 2)}s",
                    "timeout": f"{health_check_timeout}s",
                },
                "mediawiki-api-alive": {
                    "override": "replace",
                    "level": "alive",
                    "http": {"url": "http://localhost/w/api.php"},
                    "period": f"{max(10, health_check_timeout * 2)}s",
                    "timeout": f"{health_check_timeout}s",
                },
            },
        }

        self._container.add_layer(self._SERVICE_NAME, layer, combine=True)

    def _replica_relation(self) -> Relation:
        """Get the relation object for the replica peer relation.

        Raises:
            MediaWikiWaitingStatusException: If the peer relation is not ready.
        """
        replica_data = self.model.get_relation(self._PEER_RELATION_NAME)
        if replica_data is None:
            raise MediaWikiWaitingStatusException(
                f"Waiting for peer relation {self._PEER_RELATION_NAME} to be ready"
            )
        return replica_data

    def _replica_secrets(self) -> MediaWikiSecrets:
        """Get the generated secrets shared between the MediaWiki replicas.

        Raises:
            MediaWikiWaitingStatusException: If the secrets are not available yet.
        """
        try:
            secrets_content = self.model.get_secret(label=self._REPLICA_SECRET_LABEL).get_content(
                refresh=True
            )
            return MediaWikiSecrets.from_juju_secret(secrets_content)
        except SecretNotFoundError:
            raise MediaWikiWaitingStatusException("Waiting for replica secrets to be available")

    def _replica_consensus_reached(self) -> bool:
        """Check if the necessary minimal data and secrets shared with MediaWiki peers (replicas) has been initialized and synchronized."""
        try:
            self.model.get_secret(label=self._REPLICA_SECRET_LABEL)
        except SecretNotFoundError:
            return False

        return True

    def _setup_replica_data(self, _event: EventBase) -> None:
        """Initialize the synchronized data required for MediaWiki replication.

        The relation data content object is used to share (read and write) necessary secret data
        used by MediaWiki to enhance security and must be synchronized.

        Only the leader can update the data shared with all replicas.
        """
        if self._replica_consensus_reached() or not self.unit.is_leader():
            return

        logger.info("Creating replica data due to event %s", _event)
        content = MediaWikiSecrets.generate().to_juju_secret()
        self.app.add_secret(content, label=self._REPLICA_SECRET_LABEL)

    def _configure_ingress(self) -> None:
        """Configure the Traefik ingress relation.

        TODO: Switch to ingress once gateway-api-integrator supports an upstream ingress, or once connecting directly to HAProxy is viable.
        """
        if self.model.get_relation(self._INGRESS_RELATION_NAME) is None:
            return

        if not self.unit.is_leader():
            return

        if self._ingress_requirer.is_ready():
            config = self.load_charm_config()
            traefik_hostname = urlparse(config.url_origin).hostname or self.app.name

            self._ingress_requirer.submit_to_traefik(
                config={
                    "http": {
                        "routers": {
                            f"{self.app.name}-router": {
                                "rule": f"Host(`{traefik_hostname}`)",
                                "service": f"{self.app.name}-service",
                            },
                        },
                        "services": {
                            f"{self.app.name}-service": {
                                "loadBalancer": {
                                    "servers": [
                                        {
                                            "url": f"http://{self.app.name}-endpoints.{self.model.name}.svc.cluster.local:80"
                                        }
                                    ]
                                },
                            },
                        },
                    },
                }
            )
        else:
            raise MediaWikiWaitingStatusException(
                f"Waiting for {self._INGRESS_RELATION_NAME} relation to be ready"
            )

    def _reconcile_services(self) -> None:
        """Reconcile the MediaWiki services.

        The main MediaWiki (apache) service is always started.
        Job runner services are started or stopped depending on whether Redis
        is available and the job runner configuration file exists.
        """
        container = self._container
        if not container.can_connect():
            raise MediaWikiWaitingStatusException("Waiting for pebble")

        self._init_pebble_layer()
        if not self._container.get_service(self._SERVICE_NAME).is_running():
            self._container.start(self._SERVICE_NAME)

        if self._mediawiki.runner_queue_service_is_ready():
            for service in self._REDIS_JOB_SERVICES:
                if not container.get_service(service).is_running():
                    container.start(service)
        else:
            for service in self._REDIS_JOB_SERVICES:
                if (
                    service in container.get_plan().services
                    and container.get_service(service).is_running()
                ):
                    container.stop(service)

        self.unit.set_workload_version(SiteInfo.fetch().version)

    def _stop_service(self) -> None:
        """Stop the MediaWiki services."""
        container = self._container
        if not container.can_connect():
            logger.info("Cannot connect to pebble to stop service; pebble is not ready")
            return

        for service in (self._SERVICE_NAME, *self._REDIS_JOB_SERVICES):
            if (
                service in container.get_plan().services
                and container.get_service(service).is_running()
            ):
                container.stop(service)

    def _ssh_key(self, field: str) -> typing.Optional[str]:
        """Get an SSH private key from the configured user secret by field name.

        Args:
            field: The secret field name to retrieve (e.g. 'mediawiki' or 'git-sync').

        Returns:
            The private key for the requested field, or None if absent or ssh-key is not set.

        Raises:
            CharmConfigInvalidError: If the configured secret does not exist, contains none
                of the recognised fields, or the requested field's value is blank.
        """
        try:
            secret = self.load_charm_config().ssh_key
            if secret is None:
                return None
            content = secret.get_content(refresh=True)
        except SecretNotFoundError:
            raise CharmConfigInvalidError("The configured ssh-key secret does not exist.")
        except ModelError:
            raise CharmConfigInvalidError("The configured ssh-key secret is not accessible.")

        if not (content.keys() & self._SSH_KEY_FIELDS):
            raise CharmConfigInvalidError(
                "The ssh-key secret must contain at least one of: "
                + ", ".join(f"'{f}'" for f in self._SSH_KEY_FIELDS)
                + "."
            )

        value = content.get(field)
        if value is not None and not value.strip():
            raise CharmConfigInvalidError(f"The ssh-key secret field '{field}' must not be empty.")
        return value

    def _pre_reconciliation(self) -> tuple[RelationData, MediaWikiSecrets]:
        """Check for the presence of required relations and secrets, and return them.

        On error, an appropriate MediaWikiStatusException is raised, and the service is stopped.

        Returns:
            tuple[RelationData, MediaWikiSecrets]:
                RelationData: The relation data content for the replica relation.
                MediaWikiSecrets: The secrets shared between replicas.

        Raises:
            MediaWikiStatusException: If any of the required relations or secrets are not ready.
        """
        try:
            if not self._database.has_relation():
                raise MediaWikiBlockedStatusException(
                    f"Waiting for relation {self._DATABASE_RELATION_NAME}"
                )

            replica_relation = self._replica_relation()
            replica_secrets = self._replica_secrets()
        except MediaWikiStatusException as e:
            self._stop_service()
            raise e

        return replica_relation.data, replica_secrets

    def _database_reconciliation(self) -> None:
        """Complete a database schema update if requested and all units are ready.

        Does nothing if the unit calling is not the leader.

        If the self._RO_DATABASE_FLAG flag is set to "true" at the application level and for all units known to the peer relation,
        then we start the database reconciliation process.

        Once completed, the application level self._RO_DATABASE_FLAG flag is set back to "false" to allow units to return to read-write mode.

        Raises:
            MediaWikiWaitingStatusException: If the replica peer relation is not ready.
            MediaWikiWaitingStatusException: If the database update was requested but not all units have entered read-only mode yet.
        """
        if not self.unit.is_leader():
            return

        replica_relation = self._replica_relation()
        # Check if database update was requested
        if replica_relation.data[self.app].get(self._RO_DATABASE_FLAG, "false").lower() != "true":
            return
        # Check if all units are in read-only mode, which indicates they are ready for the database update
        for unit in replica_relation.units:
            unit_data = replica_relation.data[unit]
            if unit_data.get(self._RO_DATABASE_FLAG, "false").lower() != "true":
                raise MediaWikiWaitingStatusException(
                    f"Waiting for unit {unit.name} to acknowledge database update by setting ro_db to true"
                )

        original_status = self.unit.status
        self.unit.status = MaintenanceStatus("Updating database schema")
        logger.info(
            "All units have acknowledged the database update, proceeding with database schema update"
        )

        self._mediawiki.update_database_schema()

        replica_relation.data[self.app][self._RO_DATABASE_FLAG] = "false"
        self.unit.status = original_status
        logger.info("Database schema update complete")

    def _reconciliation(self, _event: EventBase) -> None:
        """Reconcile the charm state.

        This method will move the charm towards an active and correct state.

        If the container or database relation is not ready, it will not proceed.
        Pre-reconciliation steps are then taken to try and gather all the necessary data and secrets, and to validate a minimal set of prerequisites.

        Otherwise, if prerequisite criteria is met, the following actions are attempted:
        - Configure ingress.
        - Trigger the MediaWiki workload reconciliation process.
        - Flag the unit as being in read-only mode if the database was set to read-only mode.
        - Trigger the database reconciliation process.
        - Start the MediaWiki service if it is not running.
        - Configure OAuth if necessary.
        - Set the unit status to an appropriate state depending on the outcome of the above actions.

        Args:
            _event: The event that triggered the reconciliation.
        """
        self.unit.status = MaintenanceStatus("Reconciling charm state")
        logger.info("Starting reconciliation due to event: %s", _event)
        if not self._container.can_connect():
            logger.info("Reconciliation process terminated early, pebble is not ready")
            self.unit.status = WaitingStatus("Waiting for pebble")
            return

        try:
            replica_data, secrets = self._pre_reconciliation()
            set_ro_database = (
                replica_data[self.app].get(self._RO_DATABASE_FLAG, "false").lower() == "true"
            )

            self._configure_ingress()
            self._mediawiki.reconciliation(
                secrets,
                ssh_key=self._ssh_key(self._SSH_KEY_MEDIAWIKI_FIELD),
                ro_database=set_ro_database,
            )
            replica_data[self.unit][self._RO_DATABASE_FLAG] = str(set_ro_database).lower()
            self._database_reconciliation()
            self._reconcile_services()

            self._oauth.update_client_config()

        except MediaWikiStatusException as e:
            logger.info("Reconciliation process terminated early, status exception raised: %s", e)
            self.unit.status = e.status
            return
        except CharmConfigInvalidError as e:
            logger.info(
                "Reconciliation process terminated early, invalid charm configuration: %s", e
            )
            self.unit.status = BlockedStatus(str(e))
            return

        self.unit.status = (
            MaintenanceStatus("Database set to read-only mode")
            if set_ro_database
            else ActiveStatus()
        )

        logger.info("Reconciliation process complete.")

    def _on_rotate_mediawiki_secrets(self, event: ActionEvent) -> None:
        """Handle the rotate-mediawiki-secrets action.

        Rotate the secrets shared between MediaWiki replicas.

        Args:
            event: The event that triggered the secrets rotation.
        """
        logger.info("Rotating MediaWiki secrets due to event: %s", event)

        if not self.unit.is_leader():
            event.fail("Only the leader unit can rotate MediaWiki secrets")
            return

        try:
            new_secrets = MediaWikiSecrets.generate()
            secret_content = new_secrets.to_juju_secret()
            self.model.get_secret(label=self._REPLICA_SECRET_LABEL).set_content(secret_content)
            event.log("MediaWiki secrets rotated successfully")
        except SecretNotFoundError:
            event.fail("Failed to rotate secrets: replica secret not found")
        except Exception as e:
            logger.error("Failed to rotate secrets due to unexpected error: %s", e)
            event.fail("Failed to rotate secrets due to unexpected error")

    def _on_rotate_root_credentials(self, event: ActionEvent) -> None:
        """Handle the rotate-root-credentials action.

        Rotate the root bureaucrat user's credentials and ensure that it is in the bureaucrat group.
        If the user does not exist, it will be created.

        This user should only be used to assign permissions to real users, not for regular use.

        Args:
            event: The event that triggered the credential rotation.
        """
        logger.info("Rotating root bureaucrat credentials due to event: %s", event)

        try:
            new_username, new_password = self._mediawiki.rotate_root_credentials()
            event.log("Root bureaucrat user credentials rotated successfully")
            event.set_results({"username": new_username, "password": new_password})
        except MediaWikiStatusException as e:
            event.fail(f"Credential rotation failed: {e.status.message}")
        except Exception as e:
            logger.error("Credential rotation process failed with unexpected error: %s", e)
            event.fail("Credential rotation failed due to an unexpected error")

    def _on_update_database(self, event: ActionEvent) -> None:
        """Handle the update-database action.

        Request a MediaWiki database schema update.

        Args:
            event: The event that triggered the database update.
        """
        logger.info("Requesting a MediaWiki database schema update due to event: %s", event)

        if not self.unit.is_leader():
            event.fail("Only the leader unit can request a database update")
            return

        replica_data = self.model.get_relation(self._PEER_RELATION_NAME)
        if replica_data is None:
            event.fail("Peer relation not ready yet")
            return

        replica_data.data[self.app][self._RO_DATABASE_FLAG] = "true"
        event.log("Database update requested")


if __name__ == "__main__":  # pragma: nocover
    ops.main(Charm)
