# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Functions for managing and interacting with the primary MediaWiki workload/container."""

import functools
import json
import logging
import secrets
import time
from typing import TYPE_CHECKING, Callable, List, Optional, TypeVar

import mysql.connector
import ops
from charmlibs.pathops import ContainerPath

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
from mediawiki._settings import _SettingsMixin
from redis import Redis
from s3 import S3
from smtp import Smtp
from state import CharmConfig, StatefulCharmBase
from types_ import CommandExecResult

if TYPE_CHECKING:
    from mediawiki._secrets import MediaWikiSecrets

logger = logging.getLogger(__name__)

T = TypeVar("T")

INSTALLED_FLAG_TABLE = "mediawiki_charm_setup"


class MediaWiki(_ComposerMixin, _SettingsMixin, _MediaWikiBase):
    """Class to manage MediaWiki."""

    def __init__(
        self,
        charm: StatefulCharmBase,
        database: Database,
        oauth: OAuth,
        saml: Saml,
        redis: Redis,
        s3: S3,
        smtp: Smtp,
    ):
        super().__init__(charm.unit.get_container("mediawiki"))
        self._charm = charm
        self._database = database
        self._oauth = oauth
        self._saml = saml
        self._redis = redis
        self._s3 = s3
        self._smtp = smtp

    @property
    def _logs_path(self) -> ContainerPath:
        """The MediaWiki logs directory."""
        return ContainerPath(constants.LOGS_PATH, container=self._container)

    @property
    def _robots_txt_path(self) -> ContainerPath:
        """The robots.txt file served from the webroot."""
        return ContainerPath(constants.ROBOTS_TXT_PATH, container=self._container)

    @property
    def _update_wrapper_file(self) -> ContainerPath:
        """The UpdateWrapper.php file used when running update.php."""
        return ContainerPath(constants.UPDATE_WRAPPER_FILE, container=self._container)

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
        secrets: "MediaWikiSecrets",
        ssh_key: Optional[str] = None,
        ro_database: bool = False,
        force_composer_update: bool = False,
        composer_lock: Optional[str] = None,
        peer_composer_json: Optional[str] = None,
    ) -> Optional[str]:
        """Reconcile the state of MediaWiki installation and configuration.

        The following actions are completed here:
        - Ensure the logs directory exists with proper permissions.
        - Reconcile the SSH configuration for the webroot_owner user.
        - Reconcile the composer configuration, running composer update if needed.
        - Reconcile MediaWiki settings that are part of LocalSettings.php.
        - Reconcile the robots.txt file.
        - Install MediaWiki if the database is not initialized.

        Composer behaviour depends on the unit's leadership role:
        - Leaders always run ``composer update`` against the current charm config and return
          the generated lock content. ``composer_lock`` and ``peer_composer_json`` are ignored.
        - Non-leaders use ``peer_composer_json`` (the serialised composer.json the leader
          published) together with ``composer_lock`` to run ``composer install``.

        Args:
            secrets: An instance of MediaWikiSecrets containing secrets synced between units.
            ssh_key: Optional SSH private key content to write into the container for git access.
            ro_database: Whether to include settings that put the database into read-only mode
                for updates. Defaults to False.
            force_composer_update: Whether to force a composer update regardless of config
                changes. Defaults to False.
            composer_lock: The composer.lock content published by the leader. Ignored on
                leaders; required on non-leaders that have extensions configured.
            peer_composer_json: Serialised composer.json published by the leader alongside the
                lock. Ignored on leaders; used by non-leaders to ensure json+lock consistency.

        Returns:
            The current composer.lock content if this is the leader unit, None otherwise.

        Raises:
            MediaWikiStatusException: If there is a potentially transient error stopping the
                reconciliation process.
            MediaWikiInstallError: If there is an error during installation that should be
                investigated by an operator.
        """
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

    def rotate_root_credentials(self) -> tuple[str, str]:
        """Rotate the root bureaucrat user's credentials and ensure that it is in the bureaucrat group.
        If the user does not exist, it will be created.

        This user should only be used to assign permissions to real users, not for regular use.

        Returns:
            Tuple of (username, password) for the root user.

        Raises:
            MediaWikiInstallError: If there was an error creating or promoting the root user
        """
        root_password = secrets.token_urlsafe(64)
        result = self._run_maintenance_script(
            [
                "createAndPromote",
                "--bureaucrat",
                "--force",
                "--",
                constants.ROOT_USER_NAME,
                root_password,
            ],
            sensitive=True,
        )
        result.raise_for_status("Creating root user", MediaWikiInstallError)
        logger.info("Root user creation output:\n%s", result.stdout)

        return constants.ROOT_USER_NAME, root_password

    def update_database_schema(self) -> None:
        """Runs the update maintenance script, updating the MediaWiki database schema if needed.

        Should be ran after a MediaWiki upgrade, or after installing or updating an extension that requires a schema update.

        If already in a ready state, the database should be set to read only mode before running this method, and set back to read/write after completion.

        Bundled extensions listed in ``constants.BUNDLED_EXTENSIONS`` are always force-loaded so that ``update.php`` creates or migrates their tables even when they are not enabled in the normal settings.

        This is potentially dangerous action!

        Raises:
            MediaWikiInstallError: If the database update process fails.
        """
        lines = [
            "<?php",
            f'require_once "{self._local_settings_file}";',
            *(f"wfLoadExtension('{ext}');" for ext in constants.BUNDLED_EXTENSIONS),
        ]
        self._update_wrapper_file.parent.mkdir(exist_ok=True, parents=True)
        self._update_wrapper_file.write_text(
            "\n".join(lines) + "\n",
            mode=0o640,
            user=constants.ROOT_USER_NAME,
            group=constants.DAEMON_GROUP,
        )
        result = self._run_maintenance_script(["update", "--conf", str(self._update_wrapper_file)])
        result.raise_for_status("Database schema update", MediaWikiInstallError)
        logger.info("Database schema update output:\n%s", result.stdout)

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

        This includes running the MediaWiki installation script and creating a root user.
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

        self.rotate_root_credentials()
        logger.info("Completed root user creation.")

        self._set_database_initialized()

        logger.info("Completed MediaWiki installation.")

    @staticmethod
    def _db_retry_deco(func: Callable[..., T]) -> Callable[..., T]:
        """Decorator to retry a database operation with a timeout."""

        @functools.wraps(func)
        def wrapper(self: "MediaWiki") -> T:
            deadline = time.time() + constants.DB_CHECK_TIMEOUT
            while time.time() < deadline:
                try:
                    return func(self)
                except (mysql.connector.Error, MediaWikiWaitingStatusException) as e:
                    logger.warning("Database operation failed with error: %s", e)
                    time.sleep(constants.DB_CHECK_INTERVAL)
            else:
                raise MediaWikiBlockedStatusException("MySQL database operation failed")

        return wrapper

    @_db_retry_deco
    def _set_database_initialized(self) -> None:
        """Mark the MediaWiki database as initialized by creating a flag table."""
        with self._database.get_database_connection() as cnx:
            try:
                cursor = cnx.cursor()
                # Should be safe since INSTALLED_FLAG_TABLE is a constant.
                cursor.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {INSTALLED_FLAG_TABLE} (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        installed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
                cnx.commit()
                logger.debug("Marked database as initialized")
            except Exception as e:
                cnx.rollback()
                raise e

    @_db_retry_deco
    def _is_database_initialized(self) -> bool:
        """Check if the MediaWiki database has been initialized by a charm."""
        with self._database.get_database_connection() as cnx:
            cursor = cnx.cursor()
            # Should be safe since INSTALLED_FLAG_TABLE is a constant.
            cursor.execute(f"SHOW TABLES LIKE '{INSTALLED_FLAG_TABLE}'")
            result = cursor.fetchone()
            if result:
                return True
        return False

    def _run_maintenance_script(
        self,
        args: List[str],
        timeout: int = constants.LONG_TIMEOUT,
        combine_stderr: bool = False,
        sensitive: bool = False,
    ) -> CommandExecResult:
        """Execute a MediaWiki maintenance script with the given arguments.

        This is a helper method for running maintenance scripts in the form of "php maintenance/run.php <args>".

        If timeout is exceeded, a ContainerError will be raised.
        """
        result = self._run_cli(
            [str(self._php_cli_path), str(self._maintenance_scripts_base_path / "run.php"), *args],
            environment=self._charm.state.get_proxy_env(),
            user=constants.DAEMON_USER,
            group=constants.DAEMON_GROUP,
            timeout=timeout,
            combine_stderr=combine_stderr,
            sensitive=sensitive,
        )
        return result
