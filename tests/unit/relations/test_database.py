# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from collections import defaultdict

import mysql
import mysql.connector
import pytest
from mysql.connector.abstracts import MySQLConnectionAbstract
from ops import CharmBase, testing
from pytest_mock import MockerFixture, MockType

from exceptions import MediaWikiBlockedStatusException, MediaWikiWaitingStatusException
from relations import database


class WrapperCharm(CharmBase):
    """A minimal wrapper charm class to build a testing context with."""

    def __init__(self, *args):
        super().__init__(*args)
        self.database = database.Database(self, "database", "mediawiki")


@pytest.fixture
def mock_database_requires(mocker: MockerFixture) -> MockType:
    """Fixture to mock the DatabaseRequires class from charms.data_platform_libs.v0.data_interfaces."""
    mock_database_requires_cls = mocker.patch("relations.database.DatabaseRequires")
    mock_instance = mock_database_requires_cls.return_value

    return mock_instance


@pytest.fixture
def fetch_relation_data_patcher(mocker: MockerFixture) -> MockType:
    """Fixture to return a patcher for fetch_relation_data method of the DatabaseRequires instance."""
    return mocker.patch(
        "charms.data_platform_libs.v0.data_interfaces.DatabaseRequires.fetch_relation_data"
    )


@pytest.fixture
def mock_fetch_relation_data_filled(fetch_relation_data_patcher: MockType) -> MockType:
    """Fixture to mock the fetch_relation_data method with complete data."""
    # no sec B105 is bugged for multi-line dicts https://github.com/PyCQA/bandit/issues/1352
    password = "pass"  # nosec: B105

    mock_data = {
        "database": "db",
        "username": "user",
        "password": password,
        "endpoints": "host:3306",
        "read-only-endpoints": "ro-host:3306,ro-host2:3306",
    }
    fetch_relation_data_patcher.return_value = defaultdict(lambda: mock_data)
    return fetch_relation_data_patcher


@pytest.fixture
def ctx(meta: dict) -> testing.Context:
    """Provide a Context with the charm root set."""
    meta = meta.copy()
    config = meta.pop("config", None)
    actions = meta.pop("actions", None)
    return testing.Context(WrapperCharm, meta=meta, config=config, actions=actions)


class TestGetRelationData:
    """Tests for the Database.get_relation_data method."""

    @pytest.mark.usefixtures("mock_fetch_relation_data_filled")
    def test_valid_relation_data(
        self,
        ctx: testing.Context,
        active_state: testing.State,
    ):
        """Test that valid relation data is returned correctly."""
        with ctx(ctx.on.update_status(), active_state) as mgr:
            data = mgr.charm.database.get_relation_data()

            assert data is not None
            assert data.database == "db"
            assert data.username == "user"
            assert data.password == "pass"  # nosec: B105

            assert [ep.to_string() for ep in data.endpoints] == ["host:3306"]

    def test_relation_not_ready(
        self,
        ctx: testing.Context,
        base_state: testing.State,
    ):
        """Test that a MediaWikiBlockedStatusException is raised if the relation is not ready."""
        with (
            ctx(ctx.on.update_status(), base_state) as mgr,
            pytest.raises(MediaWikiBlockedStatusException, match="Waiting for relation"),
        ):
            mgr.charm.database.get_relation_data()

    def test_relation_data_not_ready(
        self,
        ctx: testing.Context,
        active_state: testing.State,
    ):
        """Test that a MediaWikiWaitingStatusException is raised if the relation data is incomplete."""
        with (
            ctx(ctx.on.update_status(), active_state) as mgr,
            pytest.raises(MediaWikiWaitingStatusException, match="Database relation is not ready"),
        ):
            mgr.charm.database.get_relation_data()

    def test_fetch_relation_data_error(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mock_fetch_relation_data_filled: MockType,
    ):
        """Test that a MediaWikiBlockedStatusException is raised if fetching relation data raises an error."""
        mock_fetch_relation_data_filled.side_effect = Exception("Mocked fetch error")
        with (
            ctx(ctx.on.update_status(), active_state) as mgr,
            pytest.raises(MediaWikiBlockedStatusException, match="Error fetching"),
        ):
            mgr.charm.database.get_relation_data()


class TestIsRelationReady:
    """Tests for the Database.is_relation_ready method."""

    @pytest.mark.usefixtures("mock_fetch_relation_data_filled")
    def test_relation_ready(
        self,
        ctx: testing.Context,
        active_state: testing.State,
    ):
        """Test that is_relation_ready returns True when relation data is valid."""
        with ctx(ctx.on.update_status(), active_state) as mgr:
            assert mgr.charm.database.is_relation_ready() is True

    def test_relation_not_ready(
        self,
        ctx: testing.Context,
        base_state: testing.State,
    ):
        """Test that is_relation_ready returns False if the relation is not ready."""
        with ctx(ctx.on.update_status(), base_state) as mgr:
            assert mgr.charm.database.is_relation_ready() is False

    def test_relation_data_not_ready(
        self,
        ctx: testing.Context,
        active_state: testing.State,
    ):
        """Test that is_relation_ready returns False if the relation data is incomplete."""
        with ctx(ctx.on.update_status(), active_state) as mgr:
            assert mgr.charm.database.is_relation_ready() is False


class TestGetDatabaseConnection:
    """Tests for the Database.get_database_connection method."""

    @pytest.mark.usefixtures("mock_fetch_relation_data_filled")
    def test_success(
        self,
        ctx: testing.Context,
        active_state: testing.State,
    ):
        """Test that get_database_connection returns a connection when relation data is valid."""
        with ctx(ctx.on.update_status(), active_state) as mgr:
            cnx = mgr.charm.database.get_database_connection()
            assert cnx is not None
            with cnx as connection:
                assert isinstance(connection, MySQLConnectionAbstract)

    @pytest.mark.usefixtures("mock_fetch_relation_data_filled")
    def test_connector_error(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mock_mysql: MockType,
    ):
        """Test that get_database_connection will pass on MySQL errors."""
        mock_mysql.side_effect = mysql.connector.Error("Connection failed", errno=-1)
        with (
            ctx(ctx.on.update_status(), active_state) as mgr,
            pytest.raises(mysql.connector.Error, match="Connection failed"),
        ):
            cnx = mgr.charm.database.get_database_connection()
            with cnx:
                pass

    def test_relation_data_not_ready(
        self,
        ctx: testing.Context,
        active_state: testing.State,
    ):
        """Test that get_database_connection raises MediaWikiWaitingStatusException if relation data is not ready."""
        with (
            ctx(ctx.on.update_status(), active_state) as mgr,
            pytest.raises(MediaWikiWaitingStatusException, match="Database relation is not ready"),
        ):
            cnx = mgr.charm.database.get_database_connection()
            with cnx:
                pass


@pytest.mark.usefixtures("mock_fetch_relation_data_filled")
class TestSetReadOnly:
    """Schema read-only changes are bounded, verified, and fail without retries."""

    @pytest.mark.parametrize("enabled", [True, False])
    @pytest.mark.parametrize("database_name", ["db", "wiki`name"])
    def test_set_read_only(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mock_fetch_relation_data_filled: MockType,
        mock_mysql: MockType,
        enabled: bool,
        database_name: str,
    ) -> None:
        """Alter only the related database on the primary and verify its schema options."""
        mock_fetch_relation_data_filled.return_value[0]["database"] = database_name
        connection = mock_mysql.return_value
        cursor = connection.cursor.return_value
        cursor.fetchone.return_value = ("READ ONLY=1" if enabled else "",)
        with ctx(ctx.on.update_status(), active_state) as mgr:
            mgr.charm.database.set_read_only(enabled)

        escaped_name = database_name.replace("`", "``")
        assert [call.args[0] for call in cursor.execute.call_args_list] == [
            "SET SESSION lock_wait_timeout = 10",
            f"ALTER DATABASE `{escaped_name}` READ ONLY = {int(enabled)}",
            "SELECT OPTIONS FROM information_schema.SCHEMATA_EXTENSIONS "
            "WHERE SCHEMA_NAME = DATABASE()",
        ]
        assert mock_mysql.call_args.kwargs["host"] == "host"
        assert mock_mysql.call_args.kwargs["connection_timeout"] == 30
        assert mock_mysql.call_args.kwargs["read_timeout"] == 30
        assert mock_mysql.call_args.kwargs["write_timeout"] == 30
        cursor.close.assert_called_once_with()
        connection.close.assert_called_once_with()

    @pytest.mark.parametrize(
        ("enabled", "row"),
        [
            (True, None),
            (False, None),
            (True, (None,)),
            (False, (None,)),
            (True, ("",)),
            (False, ("READ ONLY=1",)),
        ],
    )
    def test_verification_failure(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mock_mysql: MockType,
        enabled: bool,
        row: tuple[str | None] | None,
    ) -> None:
        """Missing or mismatched schema state never reports success or retries."""
        cursor = mock_mysql.return_value.cursor.return_value
        cursor.fetchone.return_value = row
        with (
            ctx(ctx.on.update_status(), active_state) as mgr,
            pytest.raises(MediaWikiBlockedStatusException),
        ):
            mgr.charm.database.set_read_only(enabled)
        assert cursor.execute.call_count == 3
        cursor.close.assert_called_once_with()
        mock_mysql.return_value.close.assert_called_once_with()

    @pytest.mark.parametrize("step", [0, 1, 2])
    def test_sql_failure(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mock_mysql: MockType,
        step: int,
    ) -> None:
        """A lock timeout, denied ALTER, or failed verification stops immediately."""
        cursor = mock_mysql.return_value.cursor.return_value
        cursor.execute.side_effect = [None] * step + [
            mysql.connector.Error("Operation failed", errno=1205)
        ]
        with (
            ctx(ctx.on.update_status(), active_state) as mgr,
            pytest.raises(MediaWikiBlockedStatusException, match="Operation failed"),
        ):
            mgr.charm.database.set_read_only(True)
        assert cursor.execute.call_count == step + 1
        mock_mysql.return_value.rollback.assert_not_called()
        cursor.close.assert_called_once_with()
        mock_mysql.return_value.close.assert_called_once_with()

    def test_connection_failure(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mock_mysql: MockType,
    ) -> None:
        """An unavailable database is not retried and no SQL executes."""
        mock_mysql.side_effect = mysql.connector.Error("Connection failed", errno=2003)
        with (
            ctx(ctx.on.update_status(), active_state) as mgr,
            pytest.raises(MediaWikiBlockedStatusException, match="Connection failed"),
        ):
            mgr.charm.database.set_read_only(True)
        assert mock_mysql.call_count == 1
        mock_mysql.return_value.cursor.assert_not_called()
