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
from mysql.connector.abstracts import MySQLCursorAbstract

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

# How long a schema-reconciliation DDL statement waits for the table metadata lock.
_DDL_LOCK_WAIT_TIMEOUT = 10

# Invisible surrogate column added to PRIMARY_KEY_LESS_TABLES for Group Replication
# compliance; namespaced to avoid clashing with MySQL or MediaWiki columns.
_PRIMARY_KEY_EQUIVALENT_COLUMN = "mw_charm_gr_key"


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

        self._reconcile_primary_key_compatibility()
        self._reconcile_storage_engine()

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

    @_db_retry_deco
    def _reconcile_primary_key_compatibility(self) -> None:
        """Ensure Group-Replication-incompatible core tables have a primary-key equivalent.

        A number of MediaWiki core tables historically ship without a primary key
        (see ``constants.PRIMARY_KEY_LESS_TABLES``). MySQL Group Replication rejects writes
        to such tables (error 3098, "The table does not comply with the requirements by an
        external plugin") because it requires every table to have a primary key or a non-null
        unique key.

        For each such table that still lacks a primary key and a non-null unique key, this adds
        an invisible ``BINARY(16)`` column with a per-insert UUID default and a unique key over it
        (see :meth:`_add_surrogate_key`).

        The surrogate satisfies Group Replication's primary-key-equivalent requirement without
        occupying the ``PRIMARY KEY`` slot and without leaving an ``AUTO_INCREMENT`` column behind,
        so a future MediaWiki migration that adds its own primary key (including an
        ``AUTO_INCREMENT`` one) can coexist with the surrogate. If MediaWiki later adds its own
        primary key or a non-null unique key, the now-redundant surrogate column is dropped (only
        when the table stays Group-Replication-compliant without it) by dropping its unique index
        in place and then the now-unindexed column instantly, so no table rebuild is needed; if
        that cannot be done without an expensive rebuild the harmless surrogate is left in place,
        leaving the table with just MediaWiki's schema.

        The operation is idempotent and safe to run repeatedly: missing tables are skipped,
        tables that already satisfy the requirement are skipped, and the surrogate column is
        only added when absent.

        Raises:
            MediaWikiBlockedStatusException: If the database operation fails persistently.
        """
        column = _PRIMARY_KEY_EQUIVALENT_COLUMN
        with self._database.get_database_connection() as cnx:
            try:
                cursor = cnx.cursor()
                # Bound the wait for each table's metadata lock so a contended DDL statement
                # fails fast (and is retried by the decorator) instead of blocking every query
                # that queues behind it.
                cursor.execute(f"SET SESSION lock_wait_timeout = {int(_DDL_LOCK_WAIT_TIMEOUT)};")
                for table in constants.PRIMARY_KEY_LESS_TABLES:
                    if not self._table_exists(cursor, table):
                        logger.warning(
                            "Table %s is listed in PRIMARY_KEY_LESS_TABLES but does not exist; skipping.",
                            table,
                        )
                        continue

                    has_column = self._table_has_column(cursor, table, column)
                    # Every key (the PRIMARY KEY or a non-null unique key) that satisfies Group
                    # Replication's primary-key-equivalent requirement, with its columns.
                    compliant_keys = self._group_replication_keys(cursor, table)

                    if has_column:
                        # The surrogate is redundant if any compliant key is independent of the
                        # charm-managed column (does not include it), so the table stays
                        # Group-Replication-compliant without the surrogate.
                        surrogate_redundant = any(
                            column not in cols for cols in compliant_keys.values()
                        )
                        if surrogate_redundant:
                            # The unique key needs to be dropped first as it does not support instant.
                            # We do this online to reduce cost. The column can then be dropped instantly.
                            try:
                                cursor.execute(
                                    f"ALTER TABLE `{table}` DROP INDEX `{column}_uniq`, "
                                    f"ALGORITHM=INPLACE, LOCK=NONE;"
                                )
                                cursor.execute(
                                    f"ALTER TABLE `{table}` DROP COLUMN `{column}`, "
                                    f"ALGORITHM=INSTANT;"
                                )
                                logger.info(
                                    "Dropped charm-managed surrogate key %s.%s; table now has "
                                    "its own primary key or non-null unique key.",
                                    table,
                                    column,
                                )
                            except mysql.connector.Error as exc:
                                logger.warning(
                                    "Could not cheaply drop redundant surrogate key %s.%s "
                                    "(%s); leaving it in place. It is harmless and can be "
                                    "removed later.",
                                    table,
                                    column,
                                    exc,
                                )
                        continue

                    # The table already satisfies Group Replication on its own (its own primary
                    # key or a non-null unique key) without the charm-managed surrogate. It is
                    # likely no longer PK-less and can be removed from PRIMARY_KEY_LESS_TABLES.
                    if compliant_keys:
                        logger.warning(
                            "Table %s is listed in PRIMARY_KEY_LESS_TABLES but already satisfies "
                            "the Group Replication primary-key requirement on its own; it can "
                            "likely be removed from that list.",
                            table,
                        )
                        continue

                    self._add_surrogate_key(cursor, table, column)
                cnx.commit()
            except Exception as e:
                cnx.rollback()
                raise e

    def _add_surrogate_key(self, cursor: MySQLCursorAbstract, table: str, column: str) -> None:
        """Add the charm-managed surrogate key to a Group-Replication-incompatible table.

        Group Replication rejects any write (error 3098) to a table with no primary key or
        non-null unique key. The surrogate is an invisible ``BINARY(16)`` column with a per-insert
        UUID default and its own unique key, added in three separate statements:

        1. ``ADD COLUMN ... BINARY(16) NOT NULL INVISIBLE`` (no default);
        2. ``ADD UNIQUE KEY`` over the new column;
        3. ``ALTER COLUMN ... SET DEFAULT (UUID_TO_BIN(UUID(), 1))``.

        The default cannot be set in the ``ADD COLUMN`` itself: DDL is statement-replicated even
        under ``binlog_format=ROW``, and a non-deterministic ``UUID()`` default there is rejected
        as replication-unsafe (error 1674). Setting it afterwards is a metadata-only change, and
        future rows are populated by row-logged INSERTs for which the default is replica-safe.

        The column is ``INVISIBLE`` so MediaWiki's ``SELECT *`` and ``INSERT ... SELECT`` ignore
        it, and it takes a unique key rather than the ``PRIMARY KEY`` slot so a future MediaWiki
        primary key can coexist with and supersede it.

        The ``NOT NULL`` column has no default, so it only succeeds on an empty table. That always
        holds here: Group Replication rejects writes to the non-compliant table, so it cannot have
        accumulated rows. A non-empty table is surfaced for manual intervention rather than
        corrupted. Table and column names come from trusted constants, so interpolation is safe.

        Raises:
            MediaWikiInstallError: the table already has rows, so the ``NOT NULL`` surrogate column
                cannot be added without backfilling them. This is non-transient and needs operator
                intervention.
        """
        logger.info("Adding charm-managed surrogate key %s.%s", table, column)

        if self._table_has_rows(cursor, table):
            raise MediaWikiInstallError(
                f"Cannot add a Group-Replication-compatible key to table {table}: it already has "
                "rows, so the NOT NULL surrogate column cannot be added without backfilling them. "
                "Under Group Replication a PK-less table cannot accumulate rows, so this requires "
                "manual intervention."
            )

        # Split into three statements so the non-deterministic UUID() default is never part of an
        # ADD COLUMN DDL (which MySQL rejects as replication-unsafe, error 1674): add the bare NOT
        # NULL column, build its unique key over the empty column, then attach the default as a
        # pure metadata change for future row-logged INSERTs.
        cursor.execute(
            f"ALTER TABLE `{table}` ADD COLUMN `{column}` BINARY(16) NOT NULL INVISIBLE;"
        )
        cursor.execute(f"ALTER TABLE `{table}` ADD UNIQUE KEY `{column}_uniq` (`{column}`);")
        cursor.execute(
            f"ALTER TABLE `{table}` ALTER COLUMN `{column}` SET DEFAULT (UUID_TO_BIN(UUID(), 1));"
        )

    @_db_retry_deco
    def _reconcile_storage_engine(self) -> None:
        """Convert known MyISAM core tables to InnoDB for Group Replication compatibility.

        MediaWiki ships a few tables with a hardcoded ``ENGINE=MyISAM`` (see
        ``constants.MYISAM_TABLES``). ``searchindex`` is the notable case, historically kept on
        MyISAM because FULLTEXT indexes required it. MySQL 8 supports FULLTEXT on InnoDB, and
        Group Replication rejects writes to non-InnoDB tables, so each listed table is converted
        with ``ALTER TABLE ... ENGINE=InnoDB``.

        The conversion is idempotent: a table is altered only while it still reports
        ``ENGINE=MyISAM``, so it is a no-op once converted (by a prior run or a future MediaWiki),
        and missing tables are skipped. The rebuild preserves the table's rows and rebuilds any
        FULLTEXT indexes for InnoDB.

        The rebuild takes a metadata lock, so each conversion is best-effort: ``lock_wait_timeout``
        is bounded to fail fast under contention, and a conversion that fails is logged and left
        for the next reconciliation run rather than blocking the unit.

        Raises:
            MediaWikiBlockedStatusException: If the database connection fails persistently.
        """
        with self._database.get_database_connection() as cnx:
            try:
                cursor = cnx.cursor()
                # Bound the wait for each table's metadata lock so a contended rebuild fails fast
                # (and is retried on the next run) instead of blocking queries queued behind it.
                cursor.execute(f"SET SESSION lock_wait_timeout = {int(_DDL_LOCK_WAIT_TIMEOUT)};")
                for table in constants.MYISAM_TABLES:
                    if not self._table_exists(cursor, table):
                        logger.warning(
                            "Table %s is listed in MYISAM_TABLES but does not exist; skipping.",
                            table,
                        )
                        continue

                    engine = self._table_engine(cursor, table)
                    if engine is None or engine.upper() != "MYISAM":
                        logger.debug(
                            "Table %s is on engine %s, not MyISAM; skipping conversion.",
                            table,
                            engine,
                        )
                        continue

                    try:
                        cursor.execute(f"ALTER TABLE `{table}` ENGINE=InnoDB;")
                        logger.info("Converted table %s from MyISAM to InnoDB.", table)
                    except mysql.connector.Error as exc:
                        # Best-effort remediation: a contended or rejected rebuild is left for the
                        # next reconciliation run rather than blocking the unit. searchindex on
                        # MyISAM still functions; it is only Group-Replication-incompatible.
                        logger.warning(
                            "Could not convert table %s from MyISAM to InnoDB (%s); leaving it in "
                            "place. It will be retried on the next reconciliation run.",
                            table,
                            exc,
                        )
                cnx.commit()
            except Exception as e:
                cnx.rollback()
                raise e

    @staticmethod
    def _table_exists(cursor: MySQLCursorAbstract, table: str) -> bool:
        """Return whether a table exists in the connected database."""
        cursor.execute(
            "SELECT 1 FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s LIMIT 1;",
            (table,),
        )
        return cursor.fetchone() is not None

    @staticmethod
    def _table_has_column(cursor: MySQLCursorAbstract, table: str, column: str) -> bool:
        """Return whether a table has the named column."""
        cursor.execute(
            "SELECT 1 FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
            "AND COLUMN_NAME = %s LIMIT 1;",
            (table, column),
        )
        return cursor.fetchone() is not None

    @staticmethod
    def _table_has_rows(cursor: MySQLCursorAbstract, table: str) -> bool:
        """Return whether the table contains at least one row.

        Used to guard the surrogate's ``NOT NULL`` ``ADD COLUMN`` step, which only succeeds on an
        empty table. Under Group Replication a PK-less table cannot have accumulated rows (writes
        to it are rejected), so a non-empty table is surfaced rather than corrupted. The table name
        comes from trusted constants, so interpolation is safe.
        """
        cursor.execute(f"SELECT 1 FROM `{table}` LIMIT 1;")  # noqa: S608  # nosec: B608
        return cursor.fetchone() is not None

    @staticmethod
    def _table_engine(cursor: MySQLCursorAbstract, table: str) -> Optional[str]:
        """Return the storage engine of a table, or None if the table is missing or unknown.

        Used to guard the MyISAM-to-InnoDB conversion so the ``ALTER`` only runs while the table
        still reports ``ENGINE=MyISAM``, keeping the reconciliation idempotent.
        """
        cursor.execute(
            "SELECT ENGINE FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s LIMIT 1;",
            (table,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        (engine,) = row
        return str(engine) if engine is not None else None

    @staticmethod
    def _group_replication_keys(cursor: MySQLCursorAbstract, table: str) -> dict[str, set[str]]:
        """Return every key that satisfies Group Replication, keyed by index name.

        Group Replication accepts a table that has a ``PRIMARY KEY`` or any unique key over only
        ``NOT NULL`` columns; both kinds are returned here (the primary key appears under the name
        ``PRIMARY``). The value is the set of columns that make up the key, which lets callers tell
        whether a particular key depends on the charm-managed surrogate column (and therefore
        whether the surrogate can be dropped without losing Group Replication compliance).
        """
        cursor.execute(
            "SELECT s.INDEX_NAME, s.COLUMN_NAME, c.IS_NULLABLE "
            "FROM information_schema.STATISTICS s "
            "JOIN information_schema.COLUMNS c "
            "  ON c.TABLE_SCHEMA = s.TABLE_SCHEMA "
            " AND c.TABLE_NAME = s.TABLE_NAME "
            " AND c.COLUMN_NAME = s.COLUMN_NAME "
            "WHERE s.TABLE_SCHEMA = DATABASE() AND s.TABLE_NAME = %s "
            "  AND s.NON_UNIQUE = 0;",
            (table,),
        )
        columns_by_index: dict[str, set[str]] = {}
        nullable_indexes: set[str] = set()
        for index_name, column_name, is_nullable in cursor.fetchall():
            columns_by_index.setdefault(str(index_name), set()).add(str(column_name))
            if is_nullable == "YES":
                nullable_indexes.add(str(index_name))
        return {
            name: cols for name, cols in columns_by_index.items() if name not in nullable_indexes
        }

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
