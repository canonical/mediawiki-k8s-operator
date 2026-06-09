# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Functions for managing and interacting with the primary MediaWiki workload/container."""

import dataclasses
import functools
import json
import logging
import secrets
import textwrap
import time
from typing import Any, Callable, List, Optional, TypeVar, Union, cast
from urllib.parse import urlparse

import charms.smtp_integrator.v0.smtp as smtp
import mysql.connector
import ops
from charmlibs.pathops import ContainerPath, LocalPath
from charms.saml_integrator.v0.saml import SamlRelationData
from ops import Object

import utils
from auth import OAuth, Saml
from database import Database
from exceptions import (
    MediaWikiBlockedStatusException,
    MediaWikiInstallError,
    MediaWikiStatusException,
    MediaWikiWaitingStatusException,
)
from redis import Redis
from s3 import S3
from smtp import Smtp
from state import CharmConfig, StatefulCharmBase
from types_ import CommandExecResult, PhpTemplate

logger = logging.getLogger(__name__)

T = TypeVar("T")

INSTALLED_FLAG_TABLE = "mediawiki_charm_setup"


class MediaWiki(Object):
    """Class to manage MediaWiki."""

    _DAEMON_USER = "_daemon_"
    _DAEMON_GROUP = "_daemon_"
    _ROOT_USER_NAME = "root"
    _WEBROOT_OWNER_USER = "webroot_owner"

    _BASE_TIMEOUT = 60
    _LONG_TIMEOUT = _BASE_TIMEOUT * 10
    _DB_CHECK_TIMEOUT = _BASE_TIMEOUT * 3
    _DB_CHECK_INTERVAL = 5

    # Number of times to attempt the MediaWiki installation script before giving up.
    # The install can occasionally fail due to transient atomic operation issues.
    _INSTALL_MAX_ATTEMPTS = 3
    # Short delay between installation attempts to let transient issues settle.
    _INSTALL_RETRY_INTERVAL = 2

    # Extensions bundled in the rock image that should always be loaded
    # during schema updates, regardless of whether they are configured.
    _BUNDLED_EXTENSIONS = ("PluggableAuth", "OpenIDConnect", "SimpleSAMLphp")

    _SECURE_SETTINGS_BASE_PATH = "/etc/mediawiki"

    STATIC_ASSETS_MOUNT_POINT = "/mnt/static-assets"
    STATIC_ASSETS_REPO_PATH = STATIC_ASSETS_MOUNT_POINT + "/repo"
    WEBROOT_STATIC_PATH = "/var/www/html/static"
    JOB_RUNNER_CONFIG_PATH = _SECURE_SETTINGS_BASE_PATH + "/JobRunnerConfig.json"

    # Template paths
    _local_settings_template_file = (
        LocalPath(__file__).parent / "templates" / "LocalSettings.php.template"
    )
    _late_settings_template_file = (
        LocalPath(__file__).parent / "templates" / "LateSettings.php.template"
    )

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
        self._charm = charm
        self._container = self._charm.unit.get_container("mediawiki")
        self._database = database
        self._oauth = oauth
        self._saml = saml
        self._redis = redis
        self._s3 = s3
        self._smtp = smtp

        self._webroot_path = ContainerPath("/var/www/html", container=self._container)
        self._mediawiki_path = self._webroot_path / "w"
        self._static_assets_path = ContainerPath(
            self.WEBROOT_STATIC_PATH, container=self._container
        )
        self._logs_path = ContainerPath("/var/log/mediawiki", container=self._container)

        self._robots_txt_path = self._webroot_path / "robots.txt"

        # Configuration paths
        self._user_composer_file = self._mediawiki_path / "composer.user.json"
        self._composer_lock_file = self._mediawiki_path / "composer.lock"
        self._local_settings_file = self._mediawiki_path / "LocalSettings.php"

        ## Settings outside of Webroot
        self._secure_settings_base_path = ContainerPath(
            "/etc/mediawiki", container=self._container
        )
        self._user_settings_file = self._secure_settings_base_path / "UserSettings.php"
        self._late_settings_file = self._secure_settings_base_path / "LateSettings.php"
        self._update_wrapper_file = self._secure_settings_base_path / "UpdateWrapper.php"
        self._job_runner_config = ContainerPath(
            self.JOB_RUNNER_CONFIG_PATH, container=self._container
        )

        # Script paths
        self._composer_path = ContainerPath("/usr/bin/composer", container=self._container)
        self._php_cli_path = ContainerPath("/usr/bin/php", container=self._container)
        self._maintenance_scripts_base_path = self._mediawiki_path / "maintenance"

        # webroot_owner SSH paths
        _webroot_owner_home = ContainerPath("/home/webroot_owner", container=self._container)
        self._webroot_owner_ssh_dir = _webroot_owner_home / ".ssh"
        self._webroot_owner_ssh_key = self._webroot_owner_ssh_dir / "id_charm"
        self._webroot_owner_ssh_config = self._webroot_owner_ssh_dir / "config"
        self._webroot_owner_known_hosts = self._webroot_owner_ssh_dir / "known_hosts"

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
            user=self._DAEMON_USER,
            group=self._DAEMON_GROUP,
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
                self._ROOT_USER_NAME,
                root_password,
            ],
            sensitive=True,
        )
        if result.return_code != 0:
            logger.error(
                "Creating root user failed with return code %s\nstdout: %s\nstderr: %s",
                result.return_code,
                result.stdout,
                result.stderr,
            )
            raise MediaWikiInstallError("Creating root user failed; see logs for details.")
        else:
            logger.info("Root user creation output:\n%s", result.stdout)

        return self._ROOT_USER_NAME, root_password

    def update_database_schema(self) -> None:
        """Runs the update maintenance script, updating the MediaWiki database schema if needed.

        Should be ran after a MediaWiki upgrade, or after installing or updating an extension that requires a schema update.

        If already in a ready state, the database should be set to read only mode before running this method, and set back to read/write after completion.

        Bundled extensions listed in ``_BUNDLED_EXTENSIONS`` are always force-loaded so that ``update.php`` creates or migrates their tables even when they are not enabled in the normal settings.

        This is potentially dangerous action!

        Raises:
            MediaWikiInstallError: If the database update process fails.
        """
        lines = [
            "<?php",
            f'require_once "{self._local_settings_file}";',
            *(f"wfLoadExtension('{ext}');" for ext in self._BUNDLED_EXTENSIONS),
        ]
        self._update_wrapper_file.parent.mkdir(exist_ok=True, parents=True)
        self._update_wrapper_file.write_text(
            "\n".join(lines) + "\n",
            mode=0o640,
            user=self._ROOT_USER_NAME,
            group=self._DAEMON_GROUP,
        )
        result = self._run_maintenance_script(["update", "--conf", str(self._update_wrapper_file)])
        if result.return_code != 0:
            logger.error(
                "Database schema update failed with return code %s\nstdout: %s\nstderr: %s",
                result.return_code,
                result.stdout,
                result.stderr,
            )
            raise MediaWikiInstallError("Database schema update failed; see logs for details.")
        else:
            logger.info("Database schema update output:\n%s", result.stdout)

    def runner_queue_service_is_ready(self) -> bool:
        """Returns whether or not the runner queue services should be enabled."""
        if (not self._redis.is_relation_available()) or (not self._redis.get_endpoint()):
            return False

        return self._job_runner_config.exists()

    def _ensure_static_assets_symlink(self) -> None:
        """Create or replace the symlink that exposes the git-sync storage under the webroot.

        The shared storage is mounted outside the document root at
        :attr:`STATIC_ASSETS_MOUNT_POINT`. git-sync places its worktree at
        :attr:`STATIC_ASSETS_REPO_PATH`. This creates a symlink at
        :attr:`WEBROOT_STATIC_PATH` pointing directly to that worktree so that
        checked-out assets are served under ``/static`` without the extra
        ``/repo`` path component.

        Raises:
            MediaWikiInstallError: If the symlink could not be created.
        """
        result = self._run_cli(
            ["ln", "-sfn", self.STATIC_ASSETS_REPO_PATH, self.WEBROOT_STATIC_PATH]
        )
        if result.return_code != 0:
            logger.error(
                "Creating symlink for static assets failed with return code %s\nstdout: %s\nstderr: %s",
                result.return_code,
                result.stdout,
                result.stderr,
            )
            raise MediaWikiInstallError(
                "Failed to create symlink for static assets; see logs for details."
            )

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
            owner=self._WEBROOT_OWNER_USER,
        )

    def _composer_reconciliation(
        self,
        composer_json: dict[str, Any],
        *,
        lock_content: Optional[str] = None,
        force: bool = False,
    ) -> None:
        """Reconcile the composer configuration.

        Routing is determined by the unit's leadership role:

        **Leader path**: Writes ``composer.user.json`` and runs ``composer update``.
        The ``lock_content`` parameter is ignored.

        **Non-leader path**: If ``lock_content`` is None the unit cannot proceed and raises
        :exc:`MediaWikiWaitingStatusException`. Otherwise writes both ``composer.user.json``
        and ``composer.lock`` before running ``composer install``.

        In both paths the operation is skipped when the relevant files already match their
        expected content (unless ``force`` is True).

        Args:
            composer_json: Desired content for ``composer.user.json`` as a dict.
            lock_content: Pre-generated lock file content from the peer leader. Ignored on
                the leader; required (non-None) on non-leaders.
            force: If True, skip the content-diff check and always run composer.

        Raises:
            MediaWikiWaitingStatusException: If this is a non-leader unit and no lock has
                been published by the leader yet.
            MediaWikiBlockedStatusException: If the composer command fails.
        """
        is_leader = self._charm.unit.is_leader()

        if not is_leader and lock_content is None:
            raise MediaWikiWaitingStatusException("Waiting for leader to publish composer lock")

        # Determine whether we can skip this reconciliation.
        if not force and self._should_skip_composer(
            composer_json, is_leader=is_leader, lock_content=lock_content
        ):
            logger.debug(
                "Composer configuration%s unchanged, skipping %s.",
                "" if is_leader else " and lock",
                "update" if is_leader else "install",
            )
            return

        subcommand = "update" if is_leader else "install"
        logger.info(
            "Starting composer reconciliation (%s: %s).",
            "leader" if is_leader else "non-leader",
            subcommand,
        )

        self._user_composer_file.write_text(
            json.dumps(composer_json),
            mode=0o640,
            user=self._WEBROOT_OWNER_USER,
            group=self._DAEMON_GROUP,
        )

        # Non-leaders also write the lock file before install.
        if not is_leader:
            self._composer_lock_file.write_text(
                lock_content,  # type: ignore[arg-type]  # guarded by None check above
                mode=0o640,
                user=self._WEBROOT_OWNER_USER,
                group=self._DAEMON_GROUP,
            )

        result = self._run_cli(
            [str(self._composer_path), subcommand, "--no-dev", "--optimize-autoloader"],
            user=self._WEBROOT_OWNER_USER,
            group=self._DAEMON_GROUP,
            working_dir=str(self._mediawiki_path),
            environment=self._charm.state.get_proxy_env(),
            timeout=self._LONG_TIMEOUT * 2,
        )

        if result.return_code != 0:
            logger.error(
                "Composer %s failed with return code %s\nstdout: %s\nstderr: %s",
                subcommand,
                result.return_code,
                result.stdout,
                result.stderr,
            )
            self._handle_composer_failure(composer_json, is_leader=is_leader)
            raise MediaWikiBlockedStatusException(
                f"Composer {subcommand} failed; see logs for details."
            )

        logger.info("Composer %s completed successfully:\n%s", subcommand, result.stdout)

    def _handle_composer_failure(self, composer_json: dict[str, Any], *, is_leader: bool) -> None:
        """Write a marker after a failed composer command so that the next reconciliation retries.

        For leaders, a ``_charm_error`` key is added to ``composer.user.json``.
        For non-leaders, the lock file is cleared.
        """
        if is_leader:
            failed = {**composer_json, "_charm_error": "Composer update failed"}
            self._user_composer_file.write_text(
                json.dumps(failed),
                mode=0o640,
                user=self._WEBROOT_OWNER_USER,
                group=self._DAEMON_GROUP,
            )
        else:
            self._composer_lock_file.write_text(
                "",
                mode=0o640,
                user=self._WEBROOT_OWNER_USER,
                group=self._DAEMON_GROUP,
            )

    def _should_skip_composer(
        self,
        composer_json: dict[str, Any],
        *,
        is_leader: bool,
        lock_content: Optional[str],
    ) -> bool:
        """Return whether or not composer reconciliation can be skipped.

        For leaders, this is true if the current ``composer.user.json`` matches the desired state. For non-leaders, this is true if both the current ``composer.user.json`` matches
        the desired state and the current ``composer.lock`` matches the leader-published lock.
        """
        if not self._user_composer_file.exists():
            return not composer_json
        try:
            current_json = json.loads(self._user_composer_file.read_text())
        except json.JSONDecodeError:
            return False

        if current_json != composer_json:
            return False

        if is_leader:
            return True

        if not self._composer_lock_file.exists():
            return False
        return self._composer_lock_file.read_text() == lock_content

    def _settings_reconciliation(
        self,
        config: CharmConfig,
        secrets: "MediaWikiSecrets",
        ro_database: bool = False,
    ) -> None:
        """Reconcile all the MediaWiki settings derived from LocalSettings.php.

        Args:
            config (CharmConfig): The charm configuration.
            secrets (MediaWikiSecrets): An instance of MediaWikiSecrets containing secrets synced between units.
            ro_database: Whether to include settings that put the database into read-only mode for updates. Defaults to False.

        Raises:
            MediaWikiBlockedStatusException: If S3 relation data is malformed (raised after settings are written).
        """
        self._secure_settings_base_path.mkdir(exist_ok=True, parents=True)

        self._push_user_settings(config)
        self._push_late_settings(secrets, ro_database=ro_database)
        self._push_local_settings(config)
        logger.debug("Settings reconciliation completed successfully.")

    def _push_user_settings(self, config: CharmConfig) -> None:
        """Push the user editable settings to the container."""
        self._user_settings_file.write_text(
            config.local_settings, mode=0o640, user=self._ROOT_USER_NAME, group=self._DAEMON_GROUP
        )

    def _push_late_settings(self, secrets: "MediaWikiSecrets", ro_database: bool = False) -> None:
        """Push the charm-controlled late MediaWiki settings to the container.

        Args:
            secrets (MediaWikiSecrets): An instance of MediaWikiSecrets containing secrets synced between units.
            ro_database: Whether to include settings that put the database into read-only mode for updates. Defaults to False.
        """
        self._secure_settings_base_path.mkdir(exist_ok=True, parents=True)
        content = self._late_settings_template_file.read_text()
        content += self._get_proxy_settings()
        content += self._get_database_settings()
        content += self._get_cache_settings()

        deferred_error: Optional[MediaWikiStatusException] = None

        try:
            content += self._get_auth_settings(secrets)
        except MediaWikiBlockedStatusException as e:
            logger.warning("Auth configuration incomplete: %s", e)
            deferred_error = e

        try:
            content += self._get_smtp_settings()
        except MediaWikiStatusException as e:
            logger.warning(
                "SMTP relation data is incomplete or malformed; disabling email notifications"
            )
            deferred_error = deferred_error or e
            content += "$wgEnableEmail = false;\n"

        try:
            content += self._get_s3_settings()
        except MediaWikiBlockedStatusException as e:
            logger.warning("S3 relation data is incomplete or malformed; disabling uploads")
            deferred_error = deferred_error or e
            content += "$wgEnableUploads = false;\n"

        if ro_database:
            # https://www.mediawiki.org/wiki/Manual:Upgrading#Can_my_wiki_stay_online_while_it_is_upgrading?
            content += "$adminTask = ( PHP_SAPI === 'cli' || defined( 'MEDIAWIKI_INSTALL' ) );\n"
            content += "$wgReadOnly = $adminTask ? false : 'Ongoing database update';\n"
        else:
            content += "$wgAllowSchemaUpdates = false;\n"

        for key, value in secrets.to_local_settings().items():
            content += f"{key} = '{utils.escape_php_string(value)}';\n"

        content += "?>\n"

        self._late_settings_file.write_text(
            content, mode=0o640, user=self._ROOT_USER_NAME, group=self._DAEMON_GROUP
        )

        # Raise any deferred configuration error after settings have been written to ensure
        # the config file is always in a consistent state
        if deferred_error:
            raise deferred_error

    def _push_local_settings(self, config: CharmConfig) -> None:
        """Push the base LocalSettings.php file to the container."""
        template = PhpTemplate(self._local_settings_template_file.read_text())
        server_name = config.url_origin or f"//{self._charm.app.name}"
        content = template.substitute(
            wg_server=f'"{utils.escape_php_string(server_name)}"',
        )
        content += textwrap.dedent(f"""
        require_once "{self._user_settings_file}";
        require_once "{self._late_settings_file}";
        ?>
        """)

        self._local_settings_file.write_text(
            content, mode=0o640, user=self._WEBROOT_OWNER_USER, group=self._DAEMON_GROUP
        )

    def _robots_txt_reconciliation(self, config: CharmConfig) -> None:
        """Push the robots.txt file to the container."""
        self._robots_txt_path.write_text(
            config.robots_txt, mode=0o640, user=self._ROOT_USER_NAME, group=self._DAEMON_GROUP
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

            deadline = time.time() + self._LONG_TIMEOUT
            while time.time() < deadline:
                if self._is_database_initialized():
                    return
                time.sleep(self._DB_CHECK_INTERVAL)
            else:
                raise MediaWikiBlockedStatusException(
                    "Timed out waiting for leader to perform installation"
                )

        # Blank the user settings file before installation so that extensions which behave
        # badly during install don't cause the installation script to fail.
        self._user_settings_file.write_text(
            "", mode=0o640, user=self._ROOT_USER_NAME, group=self._DAEMON_GROUP
        )
        logger.debug("User settings cleared for installation.")

        for attempt in range(1, self._INSTALL_MAX_ATTEMPTS + 1):
            result = self._run_maintenance_script(["installPreConfigured"])
            if result.return_code == 0:
                logger.info("MediaWiki installation script output:\n%s", result.stdout)
                break
            logger.error(
                "MediaWiki installation attempt %s of %s failed with return code %s\n"
                "stdout: %s\nstderr: %s",
                attempt,
                self._INSTALL_MAX_ATTEMPTS,
                result.return_code,
                result.stdout,
                result.stderr,
            )
            if attempt < self._INSTALL_MAX_ATTEMPTS:
                time.sleep(self._INSTALL_RETRY_INTERVAL)
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

    def _get_proxy_settings(self) -> str:
        """Get the current proxy settings as a string, to be inserted into a PHP file."""
        wg_http_proxy = ""

        if (proxy := self._charm.state.proxy_config) and (url := proxy.http_proxy_string):
            wg_http_proxy = f"$wgHttpProxy = '{utils.escape_php_string(url)}';\n"

        return wg_http_proxy

    def _get_database_settings(self) -> str:
        """Get the current database settings as a string, to be inserted into a PHP file.

        Returns:
            str: The database settings formatted as a PHP string.

        Raises:
            MediaWikiWaitingStatusException: If the database relation is not ready.
            MediaWikiBlockedStatusException: If the database relation is in a blocked state.
        """
        db_data = self._database.get_relation_data()

        servers_php = [
            textwrap.dedent(f"""\
            [
                'host' => '{utils.escape_php_string(db_data.endpoints[0].to_string())}',
                'dbname' => '{utils.escape_php_string(db_data.database)}',
                'user' => '{utils.escape_php_string(db_data.username)}',
                'password' => '{utils.escape_php_string(db_data.password)}',
                'type' => 'mysql',
                'flags' => DBO_DEFAULT | DBO_SSL,
                'load' => 0,
            ]""")
        ]

        servers_str = ",\n".join(servers_php)
        _servers_idt = str.maketrans({"\n": "\n" + " " * 16})
        servers_str = servers_str.translate(_servers_idt)

        content = textwrap.dedent(
            f"""
            $wgDBname = '{utils.escape_php_string(db_data.database)}';
            $wgDBservers = [
                {servers_str}
            ];
            """
        )
        return content + "\n"

    def _get_auth_settings(self, secrets: "MediaWikiSecrets") -> str:
        """Get authentication extension settings (OAuth and/or SAML) as a PHP string.

        Orchestrates the shared PluggableAuth loading and delegates to helpers
        for provider-specific configuration.

        Returns:
            str: The combined auth settings formatted as a PHP string.
        """
        extensions: list[str] = []
        pluggable_auth_entries: list[str] = []

        oauth_entry = self._get_oauth_pluggable_auth_entry()
        if oauth_entry:
            extensions.append("OpenIDConnect")
            pluggable_auth_entries.append(oauth_entry)

        saml_entry = self._get_saml_pluggable_auth_entry(secrets)
        if saml_entry:
            extensions.append("SimpleSAMLphp")
            pluggable_auth_entries.append(saml_entry)

        if not pluggable_auth_entries:
            return ""

        load_lines = "\n                ".join(
            f"wfLoadExtension( '{ext}' );" for ext in ["PluggableAuth", *extensions]
        )
        _entry_indent = "                    "
        entries_str = (",\n" + _entry_indent).join(
            e.replace("\n", "\n" + _entry_indent) for e in pluggable_auth_entries
        )

        # The auth extensions should not be loaded when running createAndPromote.php.
        # See https://www.mediawiki.org/wiki/Extension:OpenID_Connect#Known_issues
        content = textwrap.dedent(
            f"""
            $_skipAuth = PHP_SAPI === 'cli'
                && in_array( 'createAndPromote', $_SERVER['argv'] ?? [], true );

            if ( !$_skipAuth ) {{
                {load_lines}
            }}
            unset( $_skipAuth );

            $wgPluggableAuth_Config = array_replace_recursive(
                $wgPluggableAuth_Config ?? [],
                [
                    {entries_str}
                ]
            );
            """
        )

        return content + "\n"

    def _get_oauth_pluggable_auth_entry(self) -> str | None:
        """Build the PluggableAuth config entry for OpenID Connect.

        Returns:
            The PHP array literal for the entry, or None if OAuth is not configured.
        """
        provider_info = self._oauth.get_provider_info()
        if (
            not provider_info
            or provider_info.client_id is None
            or provider_info.client_secret is None
        ):
            logger.debug("OAuth relation data is incomplete or missing, skipping OAuth settings.")
            return None

        # https://www.mediawiki.org/wiki/Extension:OpenID_Connect
        data_str = textwrap.dedent(f"""\
            'providerURL' => '{utils.escape_php_string(provider_info.issuer_url)}',
            'clientID' => '{utils.escape_php_string(provider_info.client_id)}',
            'clientSecret' => '{utils.escape_php_string(provider_info.client_secret)}'""")
        if provider_info.scope:
            scopes = self._oauth.scopes() & set(provider_info.scope.split())
            unsupported_scopes = self._oauth.scopes() - scopes
            if unsupported_scopes:
                logger.warning(
                    "OAuth provider does not support requested scopes: %s",
                    ", ".join(sorted(unsupported_scopes)),
                )
            data_str += f",\n'scope' => '{utils.escape_php_string(' '.join(sorted(scopes)))}'"
        if proxy := self._charm.state.proxy_config:
            if url := proxy.https_proxy_string:
                data_str += f",\n'proxy' => '{utils.escape_php_string(url)}'"
            elif url := proxy.http_proxy_string:
                logger.info("No HTTPS proxy; falling back to HTTP proxy for OIDC.")
                data_str += f",\n'proxy' => '{utils.escape_php_string(url)}'"

        _data_idt = str.maketrans({"\n": "\n" + " " * 8})
        data_str = data_str.translate(_data_idt)

        return textwrap.dedent(
            f"""\
            'OpenIDConnect' => [
                'plugin' => 'OpenIDConnect',
                'data' => [
                    {data_str}
                ]
            ]"""
        )

    def _get_saml_pluggable_auth_entry(self, secrets: "MediaWikiSecrets") -> str | None:
        """Build the PluggableAuth config entry for SimpleSAMLphp.

        Also pushes the SimpleSAMLphp SP configuration files when SAML is active.

        Returns:
            The PHP array literal for the entry, or None if SAML is not configured.

        Raises:
            MediaWikiBlockedStatusException: If SAML is configured but Redis is not available.
        """
        saml_data = self._saml.get_relation_data()
        if not saml_data:
            logger.debug("SAML relation data is not available, skipping SAML settings.")
            return None

        if not saml_data.endpoints:
            logger.warning("SAML relation data has no endpoints, skipping SAML settings.")
            return None

        # Write SimpleSAMLphp SP configuration
        self._push_simplesamlphp_config(saml_data, secrets)

        return textwrap.dedent(
            """\
            'SimpleSAMLphp' => [
                'plugin' => 'SimpleSAMLphp',
                'data' => [
                    'authSourceId' => 'default-sp',
                ]
            ]"""
        )

    def _push_simplesamlphp_config(
        self,
        saml_data: SamlRelationData,
        secrets: "MediaWikiSecrets",
    ) -> None:
        """Push SimpleSAMLphp SP configuration files to the container.

        Writes authsources.php, config.php (Redis session store), and
        saml20-idp-remote.php (IdP metadata). If Redis is not available,
        removes config.php and raises a blocked status exception.

        Args:
            saml_data: The SAML relation data containing IdP information.
            secrets: The charm secrets, used to get the persistent SimpleSAMLphp secret salt.

        Raises:
            MediaWikiBlockedStatusException: If Redis is not available for session storage.
        """
        simplesamlphp_base = ContainerPath("/usr/share/simplesamlphp", container=self._container)
        config_dir = ContainerPath("/etc/simplesamlphp", container=self._container)
        metadata_dir = simplesamlphp_base / "metadata"

        config_dir.mkdir(exist_ok=True, parents=True)
        metadata_dir.mkdir(exist_ok=True, parents=True)

        entity_id = utils.escape_php_string(saml_data.entity_id)

        # authsources.php
        authsources_content = textwrap.dedent(f"""\
            <?php
            $config = [
                'default-sp' => [
                    'saml:SP',
                    'idp' => '{entity_id}',
                ],
            ];
            """)
        (config_dir / "authsources.php").write_text(
            authsources_content, mode=0o640, user=self._ROOT_USER_NAME, group=self._DAEMON_GROUP
        )

        # config.php — session store config; depends on Redis
        config_file = config_dir / "config.php"
        redis_endpoint = self._redis.get_endpoint()
        if not redis_endpoint:
            config_file.unlink(missing_ok=True)
            raise MediaWikiBlockedStatusException(
                "SAML requires a Redis relation for SimpleSAMLphp session storage"
            )

        secret_salt = utils.escape_php_string(secrets.saml_secret_salt)
        redis_host, redis_port = redis_endpoint.rsplit(":", 1)
        charm_config = self._charm.load_charm_config()
        url_origin = charm_config.url_origin or f"https://{self._charm.app.name}"
        baseurlpath = utils.escape_php_string(
            urlparse(f"{url_origin}/w/simplesaml/", scheme="https").geturl()
        )

        config_entries: dict[str, str] = {
            "baseurlpath": f"'{baseurlpath}'",
            "secretsalt": f"'{secret_salt}'",
            "store.type": "'redis'",
            "store.redis.host": f"'{utils.escape_php_string(redis_host)}'",
            "store.redis.port": redis_port,
            "store.redis.prefix": "'SimpleSAMLphp'",
        }

        proxy_config = self._charm.state.proxy_config
        if proxy_config and proxy_config.https_proxy_string:
            config_entries["proxy"] = (
                f"'{utils.escape_php_string(proxy_config.https_proxy_string)}'"
            )

        entries_php = "\n".join(f"    '{k}' => {v}," for k, v in config_entries.items())
        config_content = f"<?php\n$config = [\n{entries_php}\n];\n"
        config_file.write_text(
            config_content, mode=0o640, user=self._ROOT_USER_NAME, group=self._DAEMON_GROUP
        )

        # saml20-idp-remote.php — IdP metadata from the relation
        # Group endpoints by name (SingleSignOnService, SingleLogoutService)
        endpoints_by_name: dict[str, list] = {}
        for endpoint in saml_data.endpoints:
            endpoints_by_name.setdefault(endpoint.name, []).append(endpoint)

        idp_entries: list[str] = []

        for name, endpoints in endpoints_by_name.items():
            php_endpoints = []
            for ep in endpoints:
                parts = []
                if ep.url:
                    parts.append(f"'Location' => '{utils.escape_php_string(str(ep.url))}'")
                parts.append(f"'Binding' => '{utils.escape_php_string(ep.binding)}'")
                if ep.response_url:
                    parts.append(
                        f"'ResponseLocation' => '{utils.escape_php_string(str(ep.response_url))}'"
                    )
                php_endpoints.append("[" + ", ".join(parts) + "]")
            endpoints_str = ", ".join(php_endpoints)
            idp_entries.append(f"    '{name}' => [{endpoints_str}]")

        if saml_data.certificates:
            keys_php = ", ".join(
                f"['type' => 'X509Certificate', 'signing' => true,"
                f" 'encryption' => true,"
                f" 'X509Certificate' => '{utils.escape_php_string(c)}']"
                for c in saml_data.certificates
            )
            idp_entries.append(f"    'keys' => [{keys_php}]")

        entries_str = ",\n".join(idp_entries)
        idp_metadata_content = textwrap.dedent(f"""\
            <?php
            $metadata['{entity_id}'] = [
            {entries_str},
            ];
            """)
        (metadata_dir / "saml20-idp-remote.php").write_text(
            idp_metadata_content, mode=0o640, user=self._ROOT_USER_NAME, group=self._DAEMON_GROUP
        )

    def _get_cache_settings(self) -> str:
        """Get the current cache settings as a string, to be inserted into a PHP file.
        This also updates the job runner configuration as needed.

        Returns:
            str: The cache settings formatted as a PHP string.
        """
        if (not self._redis.is_relation_available()) or (
            not (endpoint := self._redis.get_endpoint())
        ):
            logger.debug(
                "Redis relation is not available or incomplete, using default cache settings."
            )
            self._job_runner_config.unlink(missing_ok=True)
            return (
                textwrap.dedent("""
                $wgMainCacheType = CACHE_NONE;
                $wgSessionCacheType = CACHE_DB;
                """)
                + "\n"
            )

        job_runner_config = {
            "groups": {
                "basic": {
                    "runners": 19,
                    "include": ["*"],
                    "low-priority": ["htmlCacheUpdate", "refreshLinks"],
                    "exclude": [
                        "AssembleUploadChunks",
                        "PublishStashedFile",
                        "uploadFromUrl",
                        "webVideoTranscode",
                        "webVideoTranscodePrioritized",
                    ],
                },
                "transcode": {"runners": 0, "include": ["webVideoTranscode"]},
                "priorityTranscode": {"runners": 0, "include": ["webVideoTranscodePrioritized"]},
                "upload": {
                    "runners": 7,
                    "include": ["AssembleUploadChunks", "PublishStashedFile", "uploadFromUrl"],
                },
            },
            "limits": {
                "attempts": {"*": 3},
                "claimTTL": {
                    "*": 3600,
                    "webVideoTranscode": 86400,
                    "webVideoTranscodePrioritized": 86400,
                },
                "real": {
                    "*": 300,
                    "webVideoTranscode": 86400,
                    "webVideoTranscodePrioritized": 86400,
                },
                "memory": {"*": "300M"},
            },
            "redis": {
                "aggregators": [endpoint],
                "queues": [endpoint],
            },
            "dispatcher": f"{self._php_cli_path} {self._maintenance_scripts_base_path / 'run.php'} runJobs --wiki=%(db)x --type=%(type)x --maxtime=%(maxtime)x --memory-limit=%(maxmem)x --result=json",
        }
        self._job_runner_config.write_text(
            json.dumps(job_runner_config, indent=4),
            mode=0o640,
            user=self._ROOT_USER_NAME,
            group=self._DAEMON_GROUP,
        )

        # https://www.mediawiki.org/wiki/Redis
        content = textwrap.dedent(
            f"""
            $wgObjectCaches['redis'] = [
                'class'                => 'RedisBagOStuff',
                'servers'              => [ '{utils.escape_php_string(endpoint)}' ],
            ];

            $wgMainCacheType = 'redis';
            $wgSessionCacheType = 'redis';

            $wgJobTypeConf['default'] = [
                'class'          => 'JobQueueRedis',
                'redisServer'    => '{utils.escape_php_string(endpoint)}',
                'redisConfig'    => [],
                'daemonized'     => true
            ];

            $wgJobRunRate = 0;
            """
        )
        return content + "\n"

    def _get_s3_settings(self) -> str:
        """Get the current S3 settings as a string, to be inserted into a PHP file.

        Note that even when S3 is available, uploads needs to explicitly enabled via LocalSettings.php.

        Returns:
            str: The S3 settings formatted as a PHP string.

        Raises:
            MediaWikiBlockedStatusException: If S3 relation data is incomplete or malformed.
        """
        if not self._s3.has_relation():
            return "$wgEnableUploads = false;\n"

        s3_data = self._s3.get_relation_data()

        # https://github.com/edwardspec/mediawiki-aws-s3
        # Note that $wgAWSRegion has to be set even if there is no region
        content = textwrap.dedent(
            f"""
            wfLoadExtension( 'AWS' );

            $wgAWSCredentials = [
                'key' => '{utils.escape_php_string(s3_data.access_key)}',
                'secret' => '{utils.escape_php_string(s3_data.secret_key)}',
                'token' => false
            ];
            $wgAWSRegion = '{utils.escape_php_string(s3_data.region or "eu-west-1")}';
            $wgAWSBucketName = '{utils.escape_php_string(s3_data.bucket)}';
            $wgFileBackends['s3']['endpoint'] = '{utils.escape_php_string(s3_data.endpoint)}';
            """
        )

        if s3_data.s3_uri_style and s3_data.s3_uri_style.lower() == "path":
            content += "$wgFileBackends['s3']['use_path_style_endpoint'] = true;\n"

        return content + "\n"

    def _get_smtp_settings(self) -> str:
        """Get the current SMTP settings as a string, to be inserted into a PHP file.

        Returns:
            str: The SMTP settings formatted as a PHP string.

        Raises:
            MediaWikiBlockedStatusException: If there is an error fetching the SMTP relation data.
            MediaWikiWaitingStatusException: If the SMTP relation is not yet available.
        """
        if not self._smtp.has_relation():
            return ""

        smtp_data = self._smtp.get_relation_data()

        host = (
            f"ssl://{smtp_data.host}"
            if smtp_data.transport_security == smtp.TransportSecurity.TLS
            else smtp_data.host
        )

        wg_smtp_entries = [
            f"'host' => '{utils.escape_php_string(host)}'",
            f"'port' => {smtp_data.port}",
            f"'auth' => {str(smtp_data.auth_type == smtp.AuthType.PLAIN).lower()}",
        ]

        if smtp_data.user is not None:
            wg_smtp_entries.append(f"'username' => '{utils.escape_php_string(smtp_data.user)}'")

        if smtp_data.password is not None:
            wg_smtp_entries.append(
                f"'password' => '{utils.escape_php_string(smtp_data.password)}'"
            )

        if smtp_data.skip_ssl_verify:
            # https://github.com/pear/Net_SMTP/blob/68420118ac8f9dfe5c4b8cac1bdb955efcd4be21/docs/guide.txt#id3
            wg_smtp_entries.append(
                "'socket_options' => array('ssl' => array('verify_peer' => false, 'verify_peer_name' => false))"
            )

        entries_str = textwrap.indent(
            textwrap.dedent("\n".join(f"{e}," for e in wg_smtp_entries)), " " * 4
        )
        content = textwrap.dedent(
            """\
            $wgSMTP = [
            {entries}
            ];
            """
        ).format(entries=entries_str)

        if smtp_data.smtp_sender is not None:
            content += f"$wgPasswordSender = '{utils.escape_php_string(smtp_data.smtp_sender)}';\n"

        return content + "\n"

    @staticmethod
    def _db_retry_deco(func: Callable[..., T]) -> Callable[..., T]:
        """Decorator to retry a database operation with a timeout."""

        @functools.wraps(func)
        def wrapper(self: "MediaWiki") -> T:
            deadline = time.time() + self._DB_CHECK_TIMEOUT
            while time.time() < deadline:
                try:
                    return func(self)
                except (mysql.connector.Error, MediaWikiWaitingStatusException) as e:
                    logger.warning("Database operation failed with error: %s", e)
                    time.sleep(self._DB_CHECK_INTERVAL)
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
        timeout: int = _LONG_TIMEOUT,
        combine_stderr: bool = False,
        sensitive: bool = False,
    ) -> CommandExecResult:
        """Execute a MediaWiki maintenance script with the given arguments.

        This is a helper method for running maintenance scripts in the form of "php maintenance/run.php <args>".

        If timeout is exceeded, an MediaWikiInstallError will be raised.
        """
        result = self._run_cli(
            [str(self._php_cli_path), str(self._maintenance_scripts_base_path / "run.php"), *args],
            environment=self._charm.state.get_proxy_env(),
            user=self._DAEMON_USER,
            group=self._DAEMON_GROUP,
            timeout=timeout,
            combine_stderr=combine_stderr,
            sensitive=sensitive,
        )
        return result

    def _run_cli(
        self,
        cmd: List[str],
        *,
        environment: dict[str, str] | None = None,
        user: Union[str, None] = None,
        group: Union[str, None] = None,
        working_dir: Union[str, None] = None,
        combine_stderr: bool = False,
        timeout: int = _BASE_TIMEOUT,
        sensitive: bool = False,
    ) -> CommandExecResult:
        """Execute a command in MediaWiki container.

        Args:
            cmd (List[str]): The command to be executed.
            environment (dict[str, str], optional): Environment variables to set for the command. Defaults to None.
            user (str): Username to run this command as, use root when not provided.
            group (str): Name of the group to run this command as, use root when not provided.
            working_dir (str):  Working dir to run this command in, use home dir if not provided.
            combine_stderr (bool): Redirect stderr to stdout, when enabled, stderr in the result
                will always be empty.
            timeout (int): Set a timeout for the running program in seconds.
                ``MediaWikiInstallError`` will be raised if timeout exceeded.
            sensitive (bool): Whether the command contains sensitive information, such as passwords. If True, the command will be redacted in logs.

        Returns:
            A named tuple with three fields: return code, stdout and stderr. Stdout and stderr are
            both string.
        """
        cmd_preview = cmd
        if sensitive:
            cmd_preview = ["REDACTED SENSITIVE COMMAND"]

        process = self._container.exec(
            cmd,
            environment=environment,
            user=user,
            group=group,
            working_dir=working_dir,
            combine_stderr=combine_stderr,
            timeout=timeout,
        )
        try:
            stdout, stderr = process.wait_output()
            result = CommandExecResult(return_code=0, stdout=stdout, stderr=stderr)
        except ops.pebble.ExecError as error:
            result = CommandExecResult(
                error.exit_code,
                cast(Union[str, bytes], error.stdout),
                cast(Union[str, bytes, None], error.stderr),
            )
        except TimeoutError:
            logger.error("Command timed out after %s seconds: %s", timeout, cmd_preview)

            raise MediaWikiInstallError(
                "Container command execution timed out; see logs for details."
            )

        return_code = result.return_code
        if combine_stderr:
            logger.debug(
                "Run command: %s return code %s\noutput: %s",
                cmd_preview,
                return_code,
                result.stdout,
            )
        else:
            logger.debug(
                "Run command: %s, return code %s\nstdout: %s\nstderr:%s",
                cmd_preview,
                return_code,
                result.stdout,
                result.stderr,
            )
        return result


@dataclasses.dataclass(frozen=True)
class MediaWikiSecrets:
    """A dataclass to hold secrets relevant to MediaWiki that need to be synced between units."""

    secret_key: str
    session_secret: str
    saml_secret_salt: str

    @classmethod
    def generate(cls) -> "MediaWikiSecrets":
        """Returns a new instance of MediaWikiSecrets with randomly generated secrets."""
        return cls(
            secret_key=secrets.token_urlsafe(64),
            session_secret=secrets.token_urlsafe(64),
            saml_secret_salt=secrets.token_hex(64),
        )

    def to_local_settings(self) -> dict[str, str]:
        """Return the secrets formatted as a dictionary of PHP variable assignments to be included in LateSettings.php."""
        return {
            "$wgSecretKey": self.secret_key,
            "$wgSessionSecret": self.session_secret,
        }

    def to_juju_secret(self) -> dict[str, str]:
        """Return the secrets formatted as a dictionary for storing in Juju secrets."""
        # Juju secrets restricts key names to lowercase alphanumerics and dashes.
        return {
            "key": self.secret_key,
            "session": self.session_secret,
            "saml-salt": self.saml_secret_salt,
        }

    @classmethod
    def from_juju_secret(cls, data: dict[str, str]) -> "MediaWikiSecrets":
        """Create an instance of MediaWikiSecrets from a Juju secret style dictionary."""
        return cls(
            secret_key=data["key"],
            session_secret=data["session"],
            saml_secret_salt=data["saml-salt"],
        )
