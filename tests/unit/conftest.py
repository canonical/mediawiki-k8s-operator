# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import dataclasses
import enum
import pathlib
import textwrap
from typing import Generator

import pytest
import yaml
from mysql.connector.abstracts import MySQLConnectionAbstract
from ops import testing
from pytest_mock import MockerFixture, MockType

from charm import Charm
from git_sync import GitSync
from mediawiki import MediaWiki, MediaWikiSecrets


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
        "--conf",
        "/etc/mediawiki/UpdateWrapper.php",
    )
    SYMLINK_STATIC_ASSETS = (
        "ln",
        "-sfn",
        MediaWiki.STATIC_ASSETS_REPO_PATH,
        MediaWiki.WEBROOT_STATIC_PATH,
    )

    def ran_in(self, exec_history: list) -> bool:
        """Return True if this command was executed (all tokens present in some command)."""
        return any(set(self.value) <= set(cmd.command) for cmd in exec_history)


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
def git_sync_mounts(tmp_path):
    """Create the mounts for the git-sync container."""
    repo_dir = tmp_path / "static-assets"
    repo_dir.mkdir(parents=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    return {
        "static_assets": testing.Mount(location=GitSync.REPO_MOUNT_POINT, source=repo_dir),
        "run_dir": testing.Mount(location="/run", source=run_dir),
    }


@pytest.fixture
def git_sync_container(git_sync_mounts: dict) -> testing.Container:
    """Return a git-sync container with a mounted repo directory."""
    return testing.Container(
        name="git-sync",
        can_connect=True,
        mounts=git_sync_mounts,
    )


@pytest.fixture
def base_state(
    mediawiki_container: testing.Container,
    git_sync_container: testing.Container,
    secrets: list[testing.Secret],
) -> testing.State:
    return testing.State(
        containers=[mediawiki_container, git_sync_container],
        storages=[testing.Storage(name="static-assets-repo")],
        secrets=secrets,
        leader=True,
    )


@pytest.fixture
def active_state(
    base_state: testing.State,
    traefik_route_relation: testing.Relation,
    database_relation: testing.Relation,
    mediawiki_replica_relation: testing.PeerRelation,
) -> testing.State:
    """Provide a state with all required relations, secrets and a pebble ready container."""
    return dataclasses.replace(
        base_state,
        relations=[traefik_route_relation, database_relation, mediawiki_replica_relation],
    )


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

    ssh_dir = tmp_path / "ssh_dir"
    ssh_dir.mkdir(parents=True)

    return {
        "install_location": testing.Mount(location="/var/www/html/w", source=install_location),
        "ssh_dir": testing.Mount(location="/home/webroot_owner/.ssh", source=ssh_dir),
    }


@pytest.fixture
def mediawiki_container(
    execs: set[testing.Exec], container_mounts: dict[str, testing.Mount]
) -> testing.Container:
    """Return a base MediaWiki container for testing."""
    return testing.Container(
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
        testing.Exec(
            ExecCmd.SYMLINK_STATIC_ASSETS.value,
            return_code=0,
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


@pytest.fixture
def mediawiki_replica_relation() -> testing.PeerRelation:
    """Return a base mediawiki-replica peer relation for testing."""
    return testing.PeerRelation(
        endpoint="mediawiki-replica",
    )


@pytest.fixture
def secrets() -> list[testing.Secret]:
    """Return a list of base charm secrets with the expected keys for testing."""
    # no sec B105 is bugged when multi-line https://github.com/PyCQA/bandit/issues/1352
    secrets = MediaWikiSecrets(
        secret_key="mock_key",
        session_secret="mock_session",
    )  # nosec: B106
    return [
        testing.Secret(
            testing.RawSecretRevisionContents(secrets.to_juju_secret()),
            label=Charm._REPLICA_SECRET_LABEL,
        )
    ]


# nosec: B106 — not real credentials; used only in unit tests.
MOCK_SSH_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nmock\n-----END OPENSSH PRIVATE KEY-----\n"


@pytest.fixture
def ssh_key_secret() -> testing.Secret:
    """Return a user-owned SSH key secret with both mediawiki and git-sync fields."""
    return testing.Secret(
        testing.RawSecretRevisionContents({"mediawiki": MOCK_SSH_KEY, "git-sync": MOCK_SSH_KEY}),
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
        "url-origin": "//wiki.example.com:30017",
        "local-settings": textwrap.dedent("""
            <?php
            $wgDefaultSkin = 'vector2022';
            ?>
            """),
    }
