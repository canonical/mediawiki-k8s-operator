# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Provides the Database class to handle database relation and state."""

import logging
import traceback
from contextlib import contextmanager
from typing import Generator

import mysql.connector
from charms.data_platform_libs.v0.data_interfaces import DatabaseRequires
from mysql.connector.abstracts import MySQLConnectionAbstract
from ops import CharmBase, Object

from exceptions import (
    MediaWikiBlockedStatusException,
    MediaWikiStatusException,
    MediaWikiWaitingStatusException,
)
from types_ import DatabaseConfig, DatabaseEndpoint

logger = logging.getLogger(__name__)


class Database(Object):
    """The Database relation handler."""

    def __init__(self, charm: CharmBase, relation_name: str, database_name: str):
        """Initialize the handler and register event handlers.

        Args:
            charm: The charm instance.
            relation_name: The name of the database relation.
            database_name: The name of the database to request.
        """
        super().__init__(charm, "database-observer")

        self.db = DatabaseRequires(
            charm=charm,
            relation_name=relation_name,
            database_name=database_name,
        )

    def get_relation_data(self) -> DatabaseConfig:
        """Get the database relation data.

        If the relation data is incomplete or malformed, an exception is raised.

        Returns:
            DatabaseConfig: The database relation data.

        Raises:
            MediaWikiBlockedStatusException: If the relation missing or the data is malformed.
            MediaWikiWaitingStatusException: If the relation data is not ready.
        """
        if not self.has_relation():
            raise MediaWikiBlockedStatusException(f"Waiting for relation {self.db.relation_name}.")

        relation_id = self.db.relations[0].id
        try:
            relation_data = self.db.fetch_relation_data()[relation_id]
        except Exception as e:
            logger.warning("Error fetching relation data for %s: %s", self.db.relation_name, e)
            raise MediaWikiBlockedStatusException(
                f"Error fetching {self.db.relation_name} relation data."
            )

        def parse_endpoints(endpoints_str: str) -> tuple[DatabaseEndpoint, ...]:
            """Parse a comma-separated string of endpoints into a list of DatabaseEndpoint."""
            parsed: tuple[DatabaseEndpoint, ...] = ()
            for ep_str in endpoints_str.split(","):
                if not ep_str.strip():
                    continue
                try:
                    parsed += (DatabaseEndpoint.from_string(ep_str),)
                except ValueError as e:
                    logger.warning("Encountered malformed endpoint: %s", e)
            return parsed

        # Parse rw endpoints from "endpoints" field
        endpoints_str = relation_data.get("endpoints", "")
        rw_endpoints = parse_endpoints(endpoints_str)

        db_config = DatabaseConfig(
            endpoints=rw_endpoints,
            database=relation_data.get("database", ""),
            username=relation_data.get("username", ""),
            password=relation_data.get("password", ""),
        )

        if not db_config.ready():
            raise MediaWikiWaitingStatusException("Database relation is not ready.")
        return db_config

    def has_relation(self) -> bool:
        """Check if the relation exists."""
        return (
            self.model.get_relation(self.db.relation_name) is not None
            and len(self.db.relations) > 0
        )

    def is_relation_ready(self) -> bool:
        """Check if the database relation is ready.

        Returns:
            bool: True if the relation is ready, False otherwise.
        """
        try:
            data = self.get_relation_data()
            return data is not None and data.ready()
        except MediaWikiStatusException:
            return False

    @contextmanager
    def get_database_connection(
        self, connection_timeout: int = 30, operation_timeout: int = 30
    ) -> Generator[MySQLConnectionAbstract, None, None]:
        """Creates or gets a database connection using current database configuration/relation.

        Args:
            connection_timeout: The connection timeout in seconds. Default is 30 seconds.
            operation_timeout: The read/write timeout in seconds. Default is 30 seconds.

        Returns:
            A database connection object.

        Raises:
            MediaWikiWaitingStatusException: If the database configuration is not ready.
            mysql.connector.Error: MySQL errors.
        """
        database = self.get_relation_data()
        cnx = None

        try:
            cnx = mysql.connector.connect(
                database=database.database,
                user=database.username,
                password=database.password,
                host=database.endpoints[0].host,
                port=database.endpoints[0].port,
                connection_timeout=connection_timeout,
                read_timeout=operation_timeout,
                write_timeout=operation_timeout,
            )
            if not isinstance(cnx, MySQLConnectionAbstract):
                # This should never happen since we're not setting up a pooled connection.
                raise Exception("Unexpected MySQL connection type.")
            yield cnx
        except mysql.connector.Error as e:
            logger.info("MySQL connection error: %s", e.errno)
            if e.errno < 0:
                logger.debug("MySQL connection failed; traceback: %s", traceback.format_exc())
            raise e
        finally:
            if cnx:
                cnx.close()
