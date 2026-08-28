# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Functions for managing and interacting with the primary MediaWiki workload/container."""

import json
import logging
import secrets
import time
from typing import TYPE_CHECKING, Optional

import ops
from charmlibs.pathops import ContainerPath
from ops import pebble

import utils
from auth import OAuth, Saml
from database import Database
from exceptions import (
    MediaWikiBlockedStatusException,
    MediaWikiInstallError,
    MediaWikiWaitingStatusException,
)
from mediawiki import constants
from mediawiki._base import _MediaWikiBase
from mediawiki._composer import _ComposerMixin
from mediawiki._database import _DatabaseMixin
from mediawiki._settings import _SettingsMixin
from mediawiki_api import SiteInfo
from mediawiki_peers import MediaWikiPeers
from redis import Redis
from s3 import S3
from smtp import Smtp
from state import CharmConfig, StatefulCharmBase

if TYPE_CHECKING:
    from mediawiki._secrets import MediaWikiSecrets

logger = logging.getLogger(__name__)


class MediaWiki(_ComposerMixin, _DatabaseMixin, _SettingsMixin, _MediaWikiBase):
    """Class to manage MediaWiki."""

    _SERVICE_NAME = "mediawiki"
    _LOGROTATE_SERVICE_NAME = "logrotate"
    _APACHE_EXPORTER_SERVICE_NAME = "apache-exporter"
    _REDIS_JOB_SERVICES = ("redisJobRunnerService", "redisJobChronService")
    _APACHE_EXPORTER_PORT = 9117
    _FRESHCLAM_SERVICE_NAME = "freshclam"
    _CLAMD_SERVICE_NAME = "clamd"
    _MEDIAWIKI_API_READY_CHECK = "mediawiki-api-ready"
    _MEDIAWIKI_CHECKS = (_MEDIAWIKI_API_READY_CHECK,)

    def __init__(
        self,
        charm: StatefulCharmBase,
        database: Database,
        oauth: OAuth,
        saml: Saml,
        redis: Redis,
        s3: S3,
        smtp: Smtp,
        peers: MediaWikiPeers,
    ):
        super().__init__(charm.unit.get_container("mediawiki"))
        self._charm = charm
        self._database = database
        self._oauth = oauth
        self._saml = saml
        self._redis = redis
        self._s3 = s3
        self._smtp = smtp
        self._peers = peers

    @property
    def _logs_path(self) -> ContainerPath:
        """The MediaWiki logs directory."""
        return ContainerPath(constants.LOGS_PATH, container=self._container)

    @property
    def _robots_txt_path(self) -> ContainerPath:
        """The robots.txt file served from the webroot."""
        return ContainerPath(constants.ROBOTS_TXT_PATH, container=self._container)

    @property
    def _webroot_owner_ssh_key(self) -> ContainerPath:
        """The webroot_owner user's SSH private key file."""
        return ContainerPath(constants.WEBROOT_OWNER_SSH_KEY, container=self._container)

    @property
    def _webroot_owner_ssh_config(self) -> ContainerPath:
        """The webroot_owner user's SSH config file."""
        return ContainerPath(constants.WEBROOT_OWNER_SSH_CONFIG, container=self._container)

    @property
    def _webroot_owner_known_hosts(self) -> ContainerPath:
        """The webroot_owner user's SSH known_hosts file."""
        return ContainerPath(constants.WEBROOT_OWNER_KNOWN_HOSTS, container=self._container)

    def reconciliation(
        self,
        ssh_key: Optional[str] = None,
        force_composer_update: bool = False,
    ) -> bool:
        """Reconcile MediaWiki configuration, peer state, database, and services.

        Args:
            ssh_key: Optional SSH private key content to write into the container for git access.
            force_composer_update: Whether to force a composer update regardless of config
                changes. Defaults to False.

        Returns:
            Whether MediaWiki remains in database read-only mode.

        Raises:
            MediaWikiStatusException: If there is a potentially transient error stopping the
                reconciliation process.
            MediaWikiInstallError: If there is an error during installation that should be
                investigated by an operator.
        """
        if not self._container.can_connect():
            raise MediaWikiWaitingStatusException("Waiting for pebble")

        try:
            if not self._database.has_relation():
                raise MediaWikiBlockedStatusException(
                    f"Waiting for relation {self._database.db.relation_name}."
                )
            peer_state = self._peers.reconciliation_state()
        except (MediaWikiBlockedStatusException, MediaWikiWaitingStatusException):
            self._reconcile_services(active=False)
            raise

        force_composer_update = force_composer_update or peer_state.force_reconciliation
        new_lock = self._reconcile_configuration(
            peer_state.secrets,
            ssh_key=ssh_key,
            ro_database=peer_state.ro_database,
            force_composer_update=force_composer_update,
            composer_lock=peer_state.composer_lock,
            peer_composer_json=peer_state.composer_json,
        )
        if new_lock is not None and self._charm.unit.is_leader():
            config = self._charm.load_charm_config()
            self._peers.publish_composer_state(new_lock, json.dumps(config.composer))
        elif new_lock is not None:
            raise MediaWikiBlockedStatusException(
                "Non-leader unit attempted to publish composer state"
            )

        self._peers.acknowledge_database_mode(read_only=peer_state.ro_database)
        self._peers.reconcile_database(self.update_database_schema)
        self._reconcile_services()
        self._oauth.update_client_config()
        return peer_state.ro_database

    def _reconcile_configuration(
        self,
        secrets: "MediaWikiSecrets",
        ssh_key: Optional[str] = None,
        ro_database: bool = False,
        force_composer_update: bool = False,
        composer_lock: Optional[str] = None,
        peer_composer_json: Optional[str] = None,
    ) -> Optional[str]:
        """Reconcile MediaWiki files, installation, and workload configuration."""
        if not self._database.is_relation_ready():
            raise MediaWikiBlockedStatusException("Database relation is not ready")
        config = self._charm.load_charm_config()

        self._logs_path.mkdir(
            exist_ok=True,
            parents=True,
            mode=0o700,
            user=constants.DAEMON_USER,
            group=constants.DAEMON_GROUP,
        )
        self._ensure_static_assets_symlink()
        self._ssh_config_reconciliation(config, ssh_key)

        # Leaders compose against the current config; non-leaders must use the composer.json
        # that the leader published alongside the lock so that the two files are always in
        # sync.  If the config already requires extensions but the leader hasn't published a
        # json+lock pair yet, the non-leader waits rather than installing a mismatched state.
        if self._charm.unit.is_leader():
            composer_json_for_reconciliation = config.composer
            composer_lock_for_reconciliation = None
        else:
            composer_lock_for_reconciliation = composer_lock
            if peer_composer_json is not None:
                try:
                    composer_json_for_reconciliation = json.loads(peer_composer_json)
                except json.JSONDecodeError:
                    logger.warning(
                        "Peer-published composer.json is not valid JSON; waiting for leader to republish."
                    )
                    raise MediaWikiWaitingStatusException(
                        "Waiting for leader to publish valid composer configuration"
                    )
            elif config.composer:
                raise MediaWikiWaitingStatusException(
                    "Waiting for leader to publish composer configuration"
                )
            else:
                composer_json_for_reconciliation = {}

        self._composer_reconciliation(
            composer_json_for_reconciliation,
            lock_content=composer_lock_for_reconciliation,
            force=force_composer_update,
        )
        self._robots_txt_reconciliation(config)

        if not self._is_database_initialized():
            self._settings_reconciliation(config, secrets, ro_database=True)
            self._install(config)

        self._settings_reconciliation(config, secrets, ro_database=ro_database)

        if self._charm.unit.is_leader():
            if not self._composer_lock_file.exists():
                raise MediaWikiBlockedStatusException("Unable to fetch Composer lock file.")
            return self._composer_lock_file.read_text()

        return None

    def _pebble_layer(self) -> pebble.LayerDict:
        """Build the Pebble layer for the MediaWiki container."""
        health_check_timeout = 5
        php_path = "/usr/bin/php"
        job_runner_service_dir = "/opt/redis-job-runner-service"
        return {
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
                    "environment": self._charm.state.get_proxy_env({}),
                },
                **{
                    service: {
                        "override": "replace",
                        "summary": f"MediaWiki {service}",
                        "command": f"{php_path} {job_runner_service_dir}/{service} --config-file={constants.JOB_RUNNER_CONFIG_PATH}",
                        "startup": "disabled",
                        "environment": self._charm.state.get_proxy_env({}),
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
                    "command": 'bash -c "while :; do sleep 3600; logrotate /etc/logrotate.d/mediawiki/logrotate.conf; done"',
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
                    "environment": self._charm.state.get_proxy_env({}),
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
                "apache-exporter-up": {
                    "override": "replace",
                    "level": "alive",
                    "http": {"url": f"http://localhost:{self._APACHE_EXPORTER_PORT}/metrics"},
                },
            },
        }

    def _reconcile_checks(self, *, active: bool) -> None:
        """Start or stop MediaWiki health checks."""
        checks = self._container.get_plan().checks
        for check in self._MEDIAWIKI_CHECKS:
            if check not in checks:
                continue
            status = self._container.get_check(check).status
            if active and status == pebble.CheckStatus.INACTIVE:
                self._container.start_checks(check)
            elif not active and status != pebble.CheckStatus.INACTIVE:
                self._container.stop_checks(check)

    def _reconcile_services(self, *, active: bool = True, restart_required: bool = False) -> None:
        """Privately reconcile MediaWiki services and checks."""
        if not self._container.can_connect():
            raise MediaWikiWaitingStatusException("Waiting for pebble")
        self._container.add_layer(self._SERVICE_NAME, self._pebble_layer(), combine=True)
        self._container.replan()

        all_conditional_services = {self._SERVICE_NAME, *self._REDIS_JOB_SERVICES}
        services_to_run = {self._SERVICE_NAME}
        mediawiki_is_running = self._container.get_service(self._SERVICE_NAME).is_running()
        if not active:
            services_to_run.clear()
            self._reconcile_checks(active=False)
        elif self.runner_queue_service_is_ready():
            services_to_run.update(self._REDIS_JOB_SERVICES)

        services_to_stop = all_conditional_services - services_to_run
        services = self._container.get_plan().services
        for service in services_to_stop:
            if service in services and self._container.get_service(service).is_running():
                self._container.stop(service)
        for service in services_to_run:
            if service in services and not self._container.get_service(service).is_running():
                self._container.start(service)
        if active and restart_required and mediawiki_is_running:
            self._container.restart(self._SERVICE_NAME)
        if active:
            self._reconcile_checks(active=True)
            self._charm.unit.set_workload_version(SiteInfo.fetch().version)

    def create_and_promote_user(
        self,
        username: str,
        *,
        generate_password: bool = False,
        sysop: bool = False,
        bureaucrat: bool = False,
        interface_admin: bool = False,
        bot: bool = False,
        force: bool = False,
        custom_groups: Optional[str] = None,
        email: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> Optional[str]:
        """Create or promote a MediaWiki user, exposing the createAndPromote.php script.

        Mirrors the options of MediaWiki's ``createAndPromote.php`` maintenance
        script. If the user already exists, ``force`` must be set to promote it,
        otherwise the script fails.

        A password is only set when ``generate_password`` is ``True``, in which
        case a secure password is generated and returned. When ``False``, the
        password is left unchanged; this is only valid when promoting an existing
        user, as creating a new user without a password fails.

        Args:
            username: The username of the user to create or promote.
            generate_password: Whether to generate and set a new secure password.
            sysop: Promote the user to the ``sysop`` group.
            bureaucrat: Promote the user to the ``bureaucrat`` group.
            interface_admin: Promote the user to the ``interface-admin`` group.
            bot: Promote the user to the ``bot`` group.
            force: Update the user if it already exists.
            custom_groups: Comma-separated list of additional groups to promote the user to.
            email: Email address to set for the user.
            reason: Reason for the account creation, recorded in the logs.

        Returns:
            The generated password if ``generate_password`` is ``True``, otherwise ``None``.

        Raises:
            MediaWikiInstallError: If there was an error creating or promoting the user.
        """
        password = secrets.token_urlsafe(64) if generate_password else None

        args = ["createAndPromote"]
        if sysop:
            args.append("--sysop")
        if bureaucrat:
            args.append("--bureaucrat")
        if interface_admin:
            args.append("--interface-admin")
        if bot:
            args.append("--bot")
        if force:
            args.append("--force")
        if custom_groups:
            args.extend(["--custom-groups", custom_groups])
        if email:
            args.extend(["--email", email])
        if reason:
            args.extend(["--reason", reason])
        args.extend(["--", username])
        if password is not None:
            args.append(password)

        result = self._run_maintenance_script(args, sensitive=True)
        result.raise_for_status("Creating user", MediaWikiInstallError, include_stderr=True)
        logger.info("User creation output:\n%s", result.stdout)

        return password

    def runner_queue_service_is_ready(self) -> bool:
        """Returns whether or not the runner queue services should be enabled."""
        if (not self._redis.is_relation_available()) or (not self._redis.get_endpoint()):
            return False

        return self._job_runner_config.exists()

    def _ensure_static_assets_symlink(self) -> None:
        """Create or replace the symlink that exposes the git-sync storage under the webroot.

        The shared storage is mounted outside the document root at
        :attr:`constants.STATIC_ASSETS_MOUNT_POINT`. git-sync places its worktree at
        :attr:`constants.STATIC_ASSETS_REPO_PATH`. This creates a symlink at
        :attr:`constants.WEBROOT_STATIC_PATH` pointing directly to that worktree so that
        checked-out assets are served under ``/static`` without the extra
        ``/repo`` path component.

        Raises:
            MediaWikiInstallError: If the symlink could not be created.
        """
        result = self._run_cli(
            ["ln", "-sfn", constants.STATIC_ASSETS_REPO_PATH, constants.WEBROOT_STATIC_PATH]
        )
        result.raise_for_status("Creating symlink for static assets", MediaWikiInstallError)

    def _ssh_config_reconciliation(self, config: CharmConfig, ssh_key: Optional[str]) -> None:
        """Configure the SSH environment for the webroot_owner user.

        - Creates ~/.ssh/ with mode 700 if it does not exist.
        - Writes the provided SSH private key to ~/.ssh/id_charm if one is given,
          or removes any existing key if none is provided.
        - Writes ~/.ssh/config with StrictHostKeyChecking, an explicit IdentityFile
          directive if a key is present, and a socat ProxyCommand if an HTTP proxy
          is configured.

        This allows tools like composer and git to clone over SSH (git@host: or
        git+ssh://) without interactive prompts, tunnelling through the proxy when
        one is present.

        Args:
            config: The charm configuration, used to get the known hosts configuration.
            ssh_key: Optional SSH private key content to write into the container.
        """
        utils.ssh_reconcile_config(
            ssh_key=ssh_key,
            key_file=self._webroot_owner_ssh_key,
            config_file=self._webroot_owner_ssh_config,
            known_hosts_file=self._webroot_owner_known_hosts,
            known_hosts_content=config.ssh_known_hosts,
            proxy_config=self._charm.state.proxy_config,
            owner=constants.WEBROOT_OWNER_USER,
        )

    def _robots_txt_reconciliation(self, config: CharmConfig) -> None:
        """Push the robots.txt file to the container."""
        self._robots_txt_path.write_text(
            config.robots_txt,
            mode=0o640,
            user=constants.ROOT_USER_NAME,
            group=constants.DAEMON_GROUP,
        )

    def _install(self, config: CharmConfig) -> None:
        """Perform installation steps that should only be run by the leader unit.
        If the unit is not the leader, this method will wait until the database is marked as initialized by the leader, with a timeout.

        This includes running the MediaWiki installation script.
        The LocalSettings.php file must be in place before this method is called.

        User local settings are cleared during installation to avoid issues with extensions
        that behave badly during installation. A database upgrade is done separately after installation to finish setting up any user enabled extensions.
        """
        if not self._charm.unit.is_leader():
            logger.debug(
                f"Unit {self._charm.unit.name} is not leader; skipping leader-only installation steps."
            )
            self._charm.unit.status = ops.WaitingStatus(
                "Waiting for leader to perform installation"
            )

            deadline = time.time() + constants.LONG_TIMEOUT
            while time.time() < deadline:
                if self._is_database_initialized():
                    return
                time.sleep(constants.DB_CHECK_INTERVAL)
            else:
                raise MediaWikiBlockedStatusException(
                    "Timed out waiting for leader to perform installation"
                )

        database_empty_at_install_start = self._is_database_empty()

        # Blank the user settings file before installation so that extensions which behave
        # badly during install don't cause the installation script to fail.
        self._user_settings_file.write_text(
            "", mode=0o640, user=constants.ROOT_USER_NAME, group=constants.DAEMON_GROUP
        )
        logger.debug("User settings cleared for installation.")

        for attempt in range(1, constants.INSTALL_MAX_ATTEMPTS + 1):
            result = self._run_maintenance_script(["installPreConfigured"])
            if result.return_code == 0:
                logger.info("MediaWiki installation script output:\n%s", result.stdout)
                break
            logger.error(
                "MediaWiki installation attempt %s of %s failed with return code %s\n"
                "stdout: %s\nstderr: %s",
                attempt,
                constants.INSTALL_MAX_ATTEMPTS,
                result.return_code,
                result.stdout,
                result.stderr,
            )
            if attempt < constants.INSTALL_MAX_ATTEMPTS:
                if database_empty_at_install_start:
                    self._reset_partially_initialized_database()
                else:
                    try:
                        self.update_database_schema()
                    except MediaWikiInstallError as e:
                        logger.warning("Database schema update before retry failed: %s", e)
                time.sleep(constants.INSTALL_RETRY_INTERVAL)
        else:
            raise MediaWikiInstallError("MediaWiki installation failed; see logs for details.")
        logger.info("Completed MediaWiki install script")

        # Restore user settings and run the database upgrade to finish setting up user enabled extensions.
        self._push_user_settings(config)
        logger.debug("User settings restored after installation.")
        self.update_database_schema()
        logger.info("Database schema updated after installation.")

        self._set_database_initialized()

        logger.info("Completed MediaWiki installation.")
