# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Database schema reconciliation logic for the MediaWiki workload."""

from __future__ import annotations

import functools
import logging
import time
from typing import Callable, Optional, Sequence, TypeVar, cast

import mysql.connector
from charmlibs.pathops import ContainerPath
from mysql.connector.abstracts import MySQLCursorAbstract

from exceptions import (
    MediaWikiBlockedStatusException,
    MediaWikiInstallError,
    MediaWikiWaitingStatusException,
)
from mediawiki import constants
from mediawiki._base import _MediaWikiBase

logger = logging.getLogger(__name__)

T = TypeVar("T")

_INSTALLED_FLAG_TABLE = "mediawiki_charm_setup"

# How long a schema-reconciliation DDL statement waits for the table metadata lock.
_DDL_LOCK_WAIT_TIMEOUT = 10

# Invisible surrogate column added to PRIMARY_KEY_LESS_TABLES for Group Replication
# compliance; namespaced to avoid clashing with MySQL or MediaWiki columns.
_PRIMARY_KEY_EQUIVALENT_COLUMN = "mw_charm_gr_key"


class _DatabaseMixin(_MediaWikiBase):
    """Mixin providing database schema reconciliation for :class:`MediaWiki`."""

    @property
    def _update_wrapper_file(self) -> ContainerPath:
        """The UpdateWrapper.php file used when running update.php."""
        return ContainerPath(constants.UPDATE_WRAPPER_FILE, container=self._container)

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

    @staticmethod
    def _db_retry_deco(func: Callable[..., T]) -> Callable[..., T]:
        """Decorator to retry a database operation with a timeout."""

        @functools.wraps(func)
        def wrapper(self: _DatabaseMixin) -> T:
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
                # Should be safe since _INSTALLED_FLAG_TABLE is a constant.
                cursor.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {_INSTALLED_FLAG_TABLE} (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        installed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
                cnx.commit()
                logger.debug("Marked database as initialized")
            except Exception:
                cnx.rollback()
                raise

    @_db_retry_deco
    def _is_database_initialized(self) -> bool:
        """Check if the MediaWiki database has been initialized by a charm."""
        with self._database.get_database_connection() as cnx:
            cursor = cnx.cursor()
            # Should be safe since _INSTALLED_FLAG_TABLE is a constant.
            cursor.execute(f"SHOW TABLES LIKE '{_INSTALLED_FLAG_TABLE}'")
            result = cursor.fetchone()
            if result:
                return True
        return False

    @_db_retry_deco
    def _is_database_empty(self) -> bool:
        """Return whether the connected database contains no tables or views."""
        with self._database.get_database_connection() as cnx:
            cursor = cnx.cursor()
            cursor.execute(
                "SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() LIMIT 1;"
            )
            return cursor.fetchone() is None

    @_db_retry_deco
    def _reset_partially_initialized_database(self) -> None:
        """Drop tables created by a failed first-time MediaWiki installation attempt."""
        with self._database.get_database_connection() as cnx:
            try:
                cursor = cnx.cursor()
                cursor.execute("SET FOREIGN_KEY_CHECKS = 0;")
                cursor.execute("SHOW FULL TABLES WHERE Table_type = 'BASE TABLE';")
                rows = cast(Sequence[Sequence[object]], cursor.fetchall())
                tables = [str(row[0]) for row in rows]
                for table in tables:
                    escaped_table = table.replace("`", "``")
                    cursor.execute(f"DROP TABLE IF EXISTS `{escaped_table}`;")
                cursor.execute("SET FOREIGN_KEY_CHECKS = 1;")
                cnx.commit()
                logger.info(
                    "Dropped %s tables from failed MediaWiki installation attempt", len(tables)
                )
            except Exception:
                cnx.rollback()
                raise

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
                        else:
                            self._complete_surrogate_key(cursor, table, column, compliant_keys)
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
            except Exception:
                cnx.rollback()
                raise

    def _complete_surrogate_key(
        self,
        cursor: MySQLCursorAbstract,
        table: str,
        column: str,
        compliant_keys: dict[str, set[str]],
    ) -> None:
        """Finish a partially-created charm-managed surrogate key."""
        if compliant_keys.get(f"{column}_uniq") != {column}:
            logger.info(
                "Adding missing unique key for charm-managed surrogate %s.%s", table, column
            )
            cursor.execute(f"ALTER TABLE `{table}` ADD UNIQUE KEY `{column}_uniq` (`{column}`);")

        if not self._column_has_default(cursor, table, column):
            logger.info("Adding missing default for charm-managed surrogate %s.%s", table, column)
            cursor.execute(
                f"ALTER TABLE `{table}` ALTER COLUMN `{column}` SET DEFAULT "
                "(UUID_TO_BIN(UUID(), 1));"
            )

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
            except Exception:
                cnx.rollback()
                raise

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
    def _column_has_default(cursor: MySQLCursorAbstract, table: str, column: str) -> bool:
        """Return whether the named column has a default value."""
        cursor.execute(
            "SELECT COLUMN_DEFAULT FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
            "AND COLUMN_NAME = %s LIMIT 1;",
            (table, column),
        )
        row = cursor.fetchone()
        if row is None:
            return False
        if isinstance(row, dict):
            return row.get("COLUMN_DEFAULT") is not None
        return row[0] is not None

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
