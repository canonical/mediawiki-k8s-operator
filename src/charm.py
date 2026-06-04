#!/usr/bin/env python3

# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm Operator for mediawiki-k8s."""

import logging
import typing
from urllib.parse import urlparse

import ops
from charms.grafana_k8s.v0.grafana_dashboard import GrafanaDashboardProvider
from charms.loki_k8s.v1.loki_push_api import LogForwarder
from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointProvider
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

from auth import OAuth, Saml
from database import Database
from exceptions import (
    CharmConfigInvalidError,
    MediaWikiBlockedStatusException,
    MediaWikiStatusException,
    MediaWikiWaitingStatusException,
)
from git_sync import GitSync
from mediawiki import MediaWiki, MediaWikiSecrets
from mediawiki_api import SiteInfo
from redis import Redis
from s3 import S3
from smtp import Smtp
from state import StatefulCharmBase
from types_ import ForceReconciliationAction

# Log messages can be retrieved using juju debug-log
logger = logging.getLogger(__name__)


class Charm(StatefulCharmBase):
    """Charm for MediaWiki on Kubernetes."""

    _CONTAINER_NAME = "mediawiki"
    _SERVICE_NAME = "mediawiki"
    _LOGROTATE_SERVICE_NAME = "logrotate"
    _APACHE_EXPORTER_SERVICE_NAME = "apache-exporter"
    _REDIS_JOB_SERVICES = ("redisJobRunnerService", "redisJobChronService")

    _APACHE_EXPORTER_PORT = 9117
    _FRESHCLAM_SERVICE_NAME = "freshclam"
    _CLAMD_SERVICE_NAME = "clamd"

    _MEDIAWIKI_API_READY_CHECK = "mediawiki-api-ready"
    _APACHE_ALIVE_CHECK = "apache-alive"
    _MEDIAWIKI_CHECKS = (_MEDIAWIKI_API_READY_CHECK, _APACHE_ALIVE_CHECK)

    _DATABASE_RELATION_NAME = "database"
    _DATABASE_NAME = "mediawiki"

    _INGRESS_RELATION_NAME = "traefik-route"

    _OAUTH_RELATION_NAME = "oauth"
    _SAML_RELATION_NAME = "saml"
    _REDIS_RELATION_NAME = "redis"
    _S3_RELATION_NAME = "s3-parameters"
    _SMTP_RELATION_NAME = "smtp"

    _PEER_RELATION_NAME = "mediawiki-replica"
    _REPLICA_SECRET_LABEL = "replica-secret"  # nosec: B105

    _RO_DATABASE_FLAG = "ro_db"
    _FORCE_RECONCILIATION_FLAG = "force_reconciliation"

    _SSH_KEY_MEDIAWIKI_FIELD = "mediawiki"
    _SSH_KEY_GIT_SYNC_FIELD = "git-sync"
    _SSH_KEY_FIELDS = frozenset({_SSH_KEY_MEDIAWIKI_FIELD, _SSH_KEY_GIT_SYNC_FIELD})

    on = RedisRelationCharmEvents()  # pyright: ignore[reportAssignmentType,reportIncompatibleMethodOverride]

    def __init__(self, *args: typing.Any):
        super().__init__(*args)

        self._database = Database(self, self._DATABASE_RELATION_NAME, self._DATABASE_NAME)
        self._oauth = OAuth(self, self._OAUTH_RELATION_NAME)
        self._saml = Saml(self, self._SAML_RELATION_NAME)
        self._redis = Redis(self, self._REDIS_RELATION_NAME)
        self._s3 = S3(self, self._S3_RELATION_NAME)
        self._smtp = Smtp(self, self._SMTP_RELATION_NAME)
        self._mediawiki = MediaWiki(
            self, self._database, self._oauth, self._saml, self._redis, self._s3, self._smtp
        )
        self._git_sync = GitSync(self)

        self._ingress_requirer = TraefikRouteRequirer(
            self,
            self.model.get_relation(self._INGRESS_RELATION_NAME),  # type: ignore[arg-type]  # https://github.com/canonical/traefik-k8s-operator/issues/448
            relation_name=self._INGRESS_RELATION_NAME,
        )

        self._grafana_dashboards = GrafanaDashboardProvider(self)
        self._logging = LogForwarder(self, relation_name="logging")
        self._metrics = MetricsEndpointProvider(
            self,
            relation_name="metrics-endpoint",
            jobs=[
                {
                    "job_name": "apache_exporter",
                    "static_configs": [{"targets": [f"*:{self._APACHE_EXPORTER_PORT}"]}],
                },
                {
                    "job_name": "git_sync",
                    "static_configs": [{"targets": [f"*:{self._git_sync.GIT_SYNC_PORT}"]}],
                },
            ],
            refresh_event=[
                self.on.mediawiki_pebble_ready,
                self.on.git_sync_pebble_ready,
            ],
        )

        self.framework.observe(self.on.leader_elected, self._setup_replica_data)

        # Reconciliation events
        reconciliation_events = [
            self.on.mediawiki_pebble_ready,
            self.on.git_sync_pebble_ready,
            self._database.db.on.database_created,
            self._database.db.on.endpoints_changed,
            self.on[self._DATABASE_RELATION_NAME].relation_broken,
            self.on[self._OAUTH_RELATION_NAME].relation_created,
            self.on[self._OAUTH_RELATION_NAME].relation_changed,
            self._oauth.oauth.on.oauth_info_changed,
            self._oauth.oauth.on.oauth_info_removed,
            self._saml.saml.on.saml_data_available,
            self.on[self._SAML_RELATION_NAME].relation_broken,
            self.on.redis_relation_updated,
            self._s3.s3.on.credentials_changed,
            self._s3.s3.on.credentials_gone,
            self.on[self._SMTP_RELATION_NAME].relation_broken,
            self._smtp.on.smtp_data_available,
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
        self.framework.observe(self.on.force_reconciliation_action, self._on_force_reconciliation)

    @property
    def _container(self) -> ops.Container:
        """Return the MediaWiki container."""
        return self.unit.get_container(self._CONTAINER_NAME)

    def _pebble_layer(self) -> pebble.LayerDict:
        """Build the Pebble layer for the MediaWiki container.

        Services that are always running (mediawikiLogs, logrotate, apache-exporter,
        freshclam, clamd) have startup set to enabled and are managed via replan.
        Conditional services (mediawiki/apache, redis job runners) have startup set
        to disabled and are explicitly started/stopped in _reconcile_services.
        """
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
                    "startup": "disabled",
                    "requires": ["mediawikiLogs"],
                    "after": ["mediawikiLogs"],
                    "environment": self.state.get_proxy_env({}),
                },
                **{
                    service: {
                        "override": "replace",
                        "summary": f"MediaWiki {service}",
                        "command": f"{php_path} {job_runner_service_dir}/{service} --config-file={self._mediawiki.JOB_RUNNER_CONFIG_PATH}",
                        "startup": "disabled",
                        "environment": self.state.get_proxy_env({}),
                    }
                    for service in self._REDIS_JOB_SERVICES
                },
                "mediawikiLogs": {
                    "override": "replace",
                    "summary": "MediaWiki logs",
                    "command": "tail -n0 -F /var/log/mediawiki/logs.log",
                    "startup": "enabled",
                },
                self._LOGROTATE_SERVICE_NAME: {
                    "override": "replace",
                    "summary": "Logrotate service",
                    "command": 'bash -c "while :; '
                    "do sleep 3600; logrotate /etc/logrotate.d/mediawiki/logrotate.conf; "
                    'done"',
                    "startup": "enabled",
                },
                self._APACHE_EXPORTER_SERVICE_NAME: {
                    "override": "replace",
                    "summary": "Apache exporter for Prometheus",
                    "command": "/usr/bin/apache_exporter",
                    "startup": "enabled",
                },
                self._FRESHCLAM_SERVICE_NAME: {
                    "override": "replace",
                    "summary": "FreshClam service",
                    "command": "/usr/bin/freshclam --daemon --foreground",
                    "startup": "enabled",
                    "environment": self.state.get_proxy_env({}),
                },
                self._CLAMD_SERVICE_NAME: {
                    "override": "replace",
                    "summary": "ClamAV Daemon service",
                    "command": "/usr/sbin/clamd --foreground",
                    "startup": "enabled",
                    "after": self._FRESHCLAM_SERVICE_NAME,
                },
            },
            "checks": {
                self._MEDIAWIKI_API_READY_CHECK: {
                    "override": "replace",
                    "level": "ready",
                    "startup": "disabled",
                    "http": {
                        "url": "http://localhost/w/api.php?action=query&format=json&prop=&meta=siteinfo&formatversion=2"
                    },
                    "period": f"{max(10, health_check_timeout * 2)}s",
                    "timeout": f"{health_check_timeout}s",
                },
                self._APACHE_ALIVE_CHECK: {
                    "override": "replace",
                    "level": "alive",
                    "startup": "disabled",
                    "http": {
                        "url": f"http://localhost:{self._APACHE_EXPORTER_PORT}/metrics",
                    },
                    "period": f"{max(10, health_check_timeout * 2)}s",
                    "timeout": f"{health_check_timeout}s",
                },
                "apache-exporter-up": {
                    "override": "replace",
                    "level": "alive",
                    "http": {
                        "url": f"http://localhost:{self._APACHE_EXPORTER_PORT}/metrics",
                    },
                },
            },
        }

        return layer

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

        If the leader detects missing fields (e.g. after an upgrade that adds new secrets),
        they are populated with freshly generated values.

        Raises:
            MediaWikiWaitingStatusException: If the secrets are not available yet.
        """
        try:
            secret = self.model.get_secret(label=self._REPLICA_SECRET_LABEL)
            secrets_content = secret.get_content(refresh=True)
            # Migrate: add any fields that were not present in older deployments.
            expected = MediaWikiSecrets.generate().to_juju_secret()
            missing_keys = expected.keys() - secrets_content.keys()
            if missing_keys:
                if not self.unit.is_leader():
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

    def _reconcile_checks(self, container: ops.Container, *, active: bool) -> None:
        """Start or stop the MediaWiki health checks.

        Args:
            container: The Pebble container.
            active: Whether checks should be running.
        """
        checks = container.get_plan().checks
        for check in self._MEDIAWIKI_CHECKS:
            if check not in checks:
                continue
            status = container.get_check(check).status
            if active and status == pebble.CheckStatus.INACTIVE:
                container.start_checks(check)
            elif not active and status != pebble.CheckStatus.INACTIVE:
                container.stop_checks(check)

    def _reconcile_services(self, *, active: bool = True) -> None:
        """Reconcile the MediaWiki services.

        Applies the Pebble layer and replans to ensure always-on services are running.
        Then explicitly starts or stops conditional services (mediawiki/apache, redis
        job runners) based on the active flag and readiness conditions.

        Ordering: checks are stopped before their dependent services are stopped,
        and checks are started only after their dependent services are started.

        Args:
            active: Whether the mediawiki (apache) service should be running.
        """
        container = self._container
        if not container.can_connect():
            raise MediaWikiWaitingStatusException("Waiting for pebble")

        container.add_layer(self._SERVICE_NAME, self._pebble_layer(), combine=True)
        container.replan()

        # Determine which conditional services should be running.
        redis_enabled = active and self._mediawiki.runner_queue_service_is_ready()
        all_conditional_services = {self._SERVICE_NAME, *self._REDIS_JOB_SERVICES}

        services_to_run = set()
        if active:
            services_to_run.add(self._SERVICE_NAME)
        if active and redis_enabled:
            services_to_run.update(self._REDIS_JOB_SERVICES)
        services_to_stop = all_conditional_services - services_to_run

        # Stop checks before stopping services to avoid health check failures
        # during shutdown.
        if not active:
            self._reconcile_checks(container, active=False)

        # Stop services that should not be running.
        services = container.get_plan().services
        for service in services_to_stop:
            if service in services and container.get_service(service).is_running():
                container.stop(service)

        # Start services that should be running.
        for service in services_to_run:
            if service in services and not container.get_service(service).is_running():
                container.start(service)

        # Start checks only after services are running.
        if active:
            self._reconcile_checks(container, active=True)
            self.unit.set_workload_version(SiteInfo.fetch().version)

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
            self._reconcile_services(active=False)
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

    def _check_and_clear_force_reconciliation_flag(self, replica_data: RelationData) -> bool:
        """Check if a forced reconciliation is requested and coordinate flag cleanup.

        Each unit checks the app-level force reconciliation flag. If set, the unit
        performs the forced reconciliation and sets its own unit-level flag to acknowledge.
        The leader clears the app-level flag once all units have acknowledged.

        Args:
            replica_data: The peer relation data bags.

        Returns:
            True if a forced reconciliation should be performed.
        """
        app_flag = (
            replica_data[self.app].get(self._FORCE_RECONCILIATION_FLAG, "false").lower() == "true"
        )
        if not app_flag:
            if (
                replica_data[self.unit].get(self._FORCE_RECONCILIATION_FLAG, "false").lower()
                == "true"
            ):
                replica_data[self.unit][self._FORCE_RECONCILIATION_FLAG] = "false"
            return False

        replica_data[self.unit][self._FORCE_RECONCILIATION_FLAG] = "true"

        if self.unit.is_leader():
            replica_relation = self._replica_relation()
            all_acked = all(
                replica_relation.data[unit].get(self._FORCE_RECONCILIATION_FLAG, "false").lower()
                == "true"
                for unit in replica_relation.units
            )
            if all_acked:
                replica_data[self.app][self._FORCE_RECONCILIATION_FLAG] = "false"
                logger.info("All units acknowledged force reconciliation, app flag cleared")

        return True

    def _reconciliation(self, _event: EventBase, *, force_composer_update: bool = False) -> None:
        """Reconcile the charm state.

        This method will move the charm towards an active and correct state.

        If the container or database relation is not ready, it will not proceed.
        Pre-reconciliation steps are then taken to try and gather all the necessary data and secrets, and to validate a minimal set of prerequisites.

        Otherwise, if prerequisite criteria is met, the following actions are attempted:
        - Configure ingress.
        - Trigger the git-sync reconciliation process
        - Trigger the MediaWiki workload reconciliation process.
        - Flag the unit as being in read-only mode if the database was set to read-only mode.
        - Trigger the database reconciliation process.
        - Start the MediaWiki service if it is not running.
        - Configure OAuth if necessary.
        - Set the unit status to an appropriate state depending on the outcome of the above actions.

        Args:
            _event: The event that triggered the reconciliation.
            force_composer_update: Whether to force a composer update regardless of config changes.
        """
        self.unit.status = MaintenanceStatus("Reconciling charm state")
        logger.info("Starting reconciliation due to event: %s", _event)
        if not self._container.can_connect():
            logger.info("Reconciliation process terminated early, pebble is not ready")
            self.unit.status = WaitingStatus("Waiting for pebble")
            return

        if not self._git_sync.is_ready():
            logger.info("Reconciliation process terminated early, git-sync sidecar is not ready")
            self.unit.status = WaitingStatus("Waiting for git-sync sidecar")
            return

        try:
            replica_data, secrets = self._pre_reconciliation()
            set_ro_database = (
                replica_data[self.app].get(self._RO_DATABASE_FLAG, "false").lower() == "true"
            )

            self._configure_ingress()
            self._git_sync.reconciliation(
                ssh_key=self._ssh_key(self._SSH_KEY_GIT_SYNC_FIELD),
            )

            force_composer_update = (
                force_composer_update
                or self._check_and_clear_force_reconciliation_flag(replica_data)
            )
            self._mediawiki.reconciliation(
                secrets,
                ssh_key=self._ssh_key(self._SSH_KEY_MEDIAWIKI_FIELD),
                ro_database=set_ro_database,
                force_composer_update=force_composer_update,
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

    def _on_force_reconciliation(self, event: ActionEvent) -> None:
        """Handle the force-reconciliation action.

        If the all-units flag is set, sets the app-level flag so all units perform a
        forced reconciliation on their next reconciliation cycle. This requires the
        action to be run on the leader unit. The reconciliation is fully async.

        Without all-units, triggers a forced reconciliation (including composer update)
        on the current unit immediately.

        Args:
            event: The event that triggered the force reconciliation.
        """
        logger.info("Force reconciliation action triggered due to event: %s", event)

        params = event.load_params(ForceReconciliationAction, errors="fail")
        if params is None:
            return
        if params.all_units:
            if not self.unit.is_leader():
                event.fail("The all-units flag requires the action to be run on the leader unit")
                return

            replica_relation = self.model.get_relation(self._PEER_RELATION_NAME)
            if replica_relation is None:
                event.fail("Peer relation not ready yet")
                return

            replica_relation.data[self.app][self._FORCE_RECONCILIATION_FLAG] = "true"
            event.log("Force reconciliation requested for all units")
            return

        self._reconciliation(event, force_composer_update=True)
        event.log("Force reconciliation completed on this unit")


if __name__ == "__main__":  # pragma: nocover
    ops.main(Charm)
