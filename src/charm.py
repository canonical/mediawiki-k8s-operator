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
    SecretNotFoundError,
    WaitingStatus,
)

from auth import OAuth, Saml
from database import Database
from exceptions import (
    CharmConfigInvalidError,
    MediaWikiInstallError,
    MediaWikiStatusException,
    MediaWikiWaitingStatusException,
)
from git_sync import GitSync
from mediawiki import MediaWiki
from mediawiki_peers import MediaWikiPeers
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

    _PEER_RELATION_NAME = MediaWikiPeers.RELATION_NAME
    _REPLICA_SECRET_LABEL = MediaWikiPeers.SECRET_LABEL
    _RO_DATABASE_FLAG = MediaWikiPeers.RO_DATABASE_FLAG
    _FORCE_RECONCILIATION_FLAG = MediaWikiPeers.FORCE_RECONCILIATION_FLAG
    _COMPOSER_JSON_KEY = MediaWikiPeers.COMPOSER_JSON_KEY
    _COMPOSER_LOCK_KEY = MediaWikiPeers.COMPOSER_LOCK_KEY

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
        self._peers = MediaWikiPeers(
            self,
            relation_name=self._PEER_RELATION_NAME,
            secret_label=self._REPLICA_SECRET_LABEL,
        )
        self._mediawiki = MediaWiki(
            self,
            self._database,
            self._oauth,
            self._saml,
            self._redis,
            self._s3,
            self._smtp,
            self._peers,
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
                }
            ],
            lookaside_jobs_callable=self._git_sync.metrics_scrape_jobs,
            refresh_event=[
                self.on.mediawiki_pebble_ready,
                self.on.git_sync_pebble_ready,
                self.on.config_changed,
            ],
        )
        self.framework.observe(self.on.leader_elected, self._peers.setup_replica_data)

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
        self.framework.observe(self.on.create_and_promote_action, self._on_create_and_promote_user)
        self.framework.observe(self.on.update_database_action, self._on_update_database)
        self.framework.observe(self.on.force_reconciliation_action, self._on_force_reconciliation)

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
        - Publish the composer state if needed (leader only).
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
        if not self._git_sync.is_ready():
            logger.info("Reconciliation process terminated early, git-sync sidecar is not ready")
            self.unit.status = WaitingStatus("Waiting for git-sync sidecar")
            return

        try:
            self._configure_ingress()
            self._git_sync.reconciliation(
                ssh_key=self._ssh_key(self._SSH_KEY_GIT_SYNC_FIELD),
            )

            set_ro_database = self._mediawiki.reconciliation(
                ssh_key=self._ssh_key(self._SSH_KEY_MEDIAWIKI_FIELD),
                force_composer_update=force_composer_update,
            )

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
            self._peers.rotate_secrets()
            event.log("MediaWiki secrets rotated successfully")
        except SecretNotFoundError:
            event.fail("Failed to rotate secrets: replica secret not found")
        except Exception as e:
            logger.error("Failed to rotate secrets due to unexpected error: %s", e)
            event.fail("Failed to rotate secrets due to unexpected error")

    def _on_create_and_promote_user(self, event: ActionEvent) -> None:
        """Handle the create-and-promote action.

        Create a new MediaWiki user, or promote an existing one when ``force``
        is set, and add it to the requested user groups. This exposes
        MediaWiki's ``createAndPromote.php`` maintenance script. When
        ``generate-password`` is set, a secure password is generated and
        returned in the action results.

        Args:
            event: The event that triggered the action.
        """
        username = event.params["username"]
        generate_password = event.params.get("generate-password", False)
        force = event.params.get("force", False)
        logger.info("Creating and promoting user '%s' due to event: %s", username, event)

        if not generate_password and not force:
            event.fail(
                "Refusing to create a user without a password. Pass generate-password=true to "
                "create a user with a generated password, or force=true to promote an existing "
                "user without changing its password."
            )
            return

        try:
            password = self._mediawiki.create_and_promote_user(
                username,
                generate_password=generate_password,
                sysop=event.params.get("sysop", False),
                bureaucrat=event.params.get("bureaucrat", False),
                interface_admin=event.params.get("interface-admin", False),
                bot=event.params.get("bot", False),
                force=force,
                custom_groups=event.params.get("custom-groups") or None,
                email=event.params.get("email") or None,
                reason=event.params.get("reason") or None,
            )
            event.log(f"User '{username}' created and promoted successfully")
            results = {"username": username}
            if password is not None:
                results["password"] = password
            event.set_results(results)
        except MediaWikiStatusException as e:
            event.fail(f"User creation failed: {e.status.message}")
        except MediaWikiInstallError as e:
            event.fail(f"User creation failed: {e}")
        except Exception as e:
            logger.error("User creation process failed with unexpected error: %s", e)
            event.fail("User creation failed due to an unexpected error")

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

        if not self._peers.request_database_update():
            event.fail("Peer relation not ready yet")
            return
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

            if not self._peers.request_force_reconciliation():
                event.fail("Peer relation not ready yet")
                return
            event.log("Force reconciliation requested for all units")
            return

        self._reconciliation(event, force_composer_update=True)
        event.log("Force reconciliation completed on this unit")


if __name__ == "__main__":  # pragma: nocover
    ops.main(Charm)
