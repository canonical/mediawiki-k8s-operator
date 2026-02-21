# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import dataclasses
import enum
import pathlib
import textwrap
from typing import Generator

import pytest
import scenario  # testing.Container isn't namespaced correctly, so we import scenario directly
import yaml
from mysql.connector.abstracts import MySQLConnectionAbstract
from ops import testing
from pytest_mock import MockerFixture, MockType

from charm import Charm


class ExecCmd(enum.Enum):
    """Command prefixes for executed commands."""

    COMPOSER_UPDATE = (
        "/usr/bin/composer",
        "update",
        "--no-dev",
    )
    MAINTENANCE_INSTALL_PRE_CONFIGURED = (
        "/usr/bin/php",
        "/var/www/html/w/maintenance/run.php",
        "installPreConfigured",
    )
    MAINTENANCE_CREATE_AND_PROMOTE = (
        "/usr/bin/php",
        "/var/www/html/w/maintenance/run.php",
        "createAndPromote",
    )
    MAINTENANCE_UPDATE = (
        "/usr/bin/php",
        "/var/www/html/w/maintenance/run.php",
        "update",
    )


@pytest.fixture
def meta() -> dict:
    """Returns the charm metadata from charmcraft.yaml as a dict."""
    with open(pathlib.Path(__file__).parent.parent.parent / "charmcraft.yaml") as f:
        data = yaml.safe_load(f)
    return data


@pytest.fixture
def ctx() -> testing.Context:
    """Provide a Context with the charm root set."""
    return testing.Context(Charm)


@pytest.fixture
def base_state(mediawiki_container: scenario.Container) -> testing.State:
    return testing.State(containers=[mediawiki_container], leader=True)


@pytest.fixture
def active_state(
    base_state: testing.State,
    traefik_route_relation: testing.Relation,
    database_relation: testing.Relation,
) -> testing.State:
    """Provide a state with all required relations and a pebble ready container."""
    return dataclasses.replace(base_state, relations=[traefik_route_relation, database_relation])


@pytest.fixture
def configured_state(
    active_state: testing.State, populated_config: dict[str, bool | float | int | str]
) -> testing.State:
    """Provide an active state with a populated, valid config."""
    return dataclasses.replace(active_state, config=populated_config)


@pytest.fixture
def container_mounts(tmp_path):
    install_location = tmp_path / "install_location"
    install_location.mkdir(parents=True)

    return {
        "install_location": testing.Mount(location="/var/www/html/w", source=install_location),
    }


@pytest.fixture
def mediawiki_container(
    execs: set[testing.Exec], container_mounts: dict[str, testing.Mount]
) -> scenario.Container:
    """Return a base MediaWiki container for testing."""
    return scenario.Container(
        name=Charm._CONTAINER_NAME,
        can_connect=True,
        execs=execs,
        mounts=container_mounts,
    )


@pytest.fixture
def execs() -> Generator[set[testing.Exec], None, None]:
    yield {
        testing.Exec(
            ExecCmd.COMPOSER_UPDATE.value,
            return_code=0,
            stdout="Mocked composer update",
            stderr="",
        ),
        testing.Exec(
            ExecCmd.MAINTENANCE_INSTALL_PRE_CONFIGURED.value,
            return_code=0,
            stdout="Mocked maintenance installPreConfigured",
            stderr="",
        ),
        testing.Exec(
            ExecCmd.MAINTENANCE_CREATE_AND_PROMOTE.value,
            return_code=0,
            stdout="Mocked maintenance createAndPromote",
            stderr="",
        ),
        testing.Exec(
            ExecCmd.MAINTENANCE_UPDATE.value,
            return_code=0,
            stdout="Mocked maintenance update",
            stderr="",
        ),
    }


@pytest.fixture
def traefik_route_relation() -> testing.Relation:
    """Return a base Traefik route relation for testing."""
    return testing.Relation(
        endpoint="traefik-route",
        interface="traefik-route",
    )


@pytest.fixture
def database_relation() -> testing.Relation:
    """Return a base database relation for testing."""
    return testing.Relation(
        endpoint="database",
        interface="mysql_client",
    )


@pytest.fixture(autouse=True)
def mock_mysql(mocker: MockerFixture) -> MockType:
    """Default MySQL connector mock."""
    mock_connect = mocker.patch("mysql.connector.connect")
    mock_connection = mocker.MagicMock(spec=MySQLConnectionAbstract)
    mock_connect.return_value = mock_connection

    mock_cursor = mock_connection.cursor.return_value
    mock_cursor.execute.return_value = None
    mock_cursor.callproc.return_value = None
    mock_cursor.fetchall.return_value = []
    mock_cursor.fetchone.return_value = None

    return mock_connect


@pytest.fixture
def populated_config() -> dict[str, bool | float | int | str]:
    """Example of a populated and valid charm configuration."""
    return {
        "composer": textwrap.dedent("""
            {
                "require": {
                    "mediawiki/semantic-media-wiki": "^3.2"
                }
            }
            """),
        "robots-txt": textwrap.dedent("""
            User-agent: *
            Allow: /w/load.php?
            Disallow: /w/
            """),
        "static-assets-git-repo": "",
        "static-assets-git-ref": "",
        "hostname": "wiki.example.com:30017",
        "local-settings": textwrap.dedent("""
            <?php
            $wgDefaultSkin = 'vector2022';
            ?>
            """),
    }
