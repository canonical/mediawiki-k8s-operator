# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.


import dataclasses

import pytest
import requests
import scenario
from ops import testing
from pytest_mock import MockerFixture, MockType

import database
from charm import Charm
from exceptions import MediaWikiInstallError
from mediawiki import MediaWiki, MediaWikiSecrets
from state import CharmConfigInvalidError, StatefulCharmBase
from tests.unit.conftest import ExecCmd
from types_ import DatabaseConfig, DatabaseEndpoint


class WrapperCharm(StatefulCharmBase):
    """A minimal wrapper charm class to build a testing context with."""

    def __init__(self, *args):
        super().__init__(*args)
        self.database = database.Database(self, "database", Charm._CONTAINER_NAME)
        self.mediawiki = MediaWiki(self, self.database)


@pytest.fixture(autouse=True)
def mock_database(mocker: MockerFixture) -> MockType:
    """Base database class mock."""
    mock_database_cls = mocker.patch("database.Database", autospec=True)
    mock_instance = mock_database_cls.return_value

    mock_instance.get_relation_data.return_value = DatabaseConfig(
        endpoints=(DatabaseEndpoint.from_string("mocked-endpoint:2222"),),
        database="mocked-database",
        username="mocked-user",
        password="mocked-password",  # nosec: B106
    )
    mock_instance.is_relation_ready.return_value = True

    return mock_instance


@pytest.fixture(autouse=True)
def mock_database_cursor(mock_database: MockType) -> MockType:
    """Mock the database cursor to return expected values for SQL queries."""
    mock_cursor = mock_database.get_database_connection.return_value.__enter__.return_value.cursor.return_value
    mock_cursor.fetchone.return_value = None

    return mock_cursor


@pytest.fixture
def ctx(meta: dict) -> testing.Context:
    """Provide a Context with the charm root set."""
    meta = meta.copy()
    config = meta.pop("config", None)
    actions = meta.pop("actions", None)
    return testing.Context(WrapperCharm, meta=meta, config=config, actions=actions)


class TestReconciliation:
    def test_initial(self, ctx: testing.Context, active_state: testing.State, meta: dict) -> None:
        """Test that reconciliation runs successfully as a leader unit with required relations."""
        with ctx(ctx.on.update_status(), active_state) as mgr:
            mgr.charm.mediawiki.reconciliation(MediaWikiSecrets.generate())

            state_out = mgr.run()

        assert not any(
            "/usr/bin/composer" in cmd.command for cmd in ctx.exec_history[Charm._CONTAINER_NAME]
        ), "Did not expect composer command in exec history"
        assert any(
            "installPreConfigured" in cmd.command
            for cmd in ctx.exec_history[Charm._CONTAINER_NAME]
        ), "Expected installPreConfigured in exec history"
        assert any(
            "createAndPromote" in cmd.command for cmd in ctx.exec_history[Charm._CONTAINER_NAME]
        ), "Expected createAndPromote in exec history"

        validate_container(ctx, state_out, meta=meta)

    def test_initial_with_valid_config(
        self, ctx: testing.Context, configured_state: testing.State
    ) -> None:
        """Test that reconciliation runs successfully with a valid config."""
        with ctx(ctx.on.update_status(), configured_state) as mgr:
            mgr.charm.mediawiki.reconciliation(MediaWikiSecrets.generate())

            state_out = mgr.run()

        assert any(
            "/usr/bin/composer" in cmd.command for cmd in ctx.exec_history[Charm._CONTAINER_NAME]
        ), "Expected composer command in exec history"
        assert any(
            "installPreConfigured" in cmd.command
            for cmd in ctx.exec_history[Charm._CONTAINER_NAME]
        ), "Expected installPreConfigured in exec history"
        assert any(
            "createAndPromote" in cmd.command for cmd in ctx.exec_history[Charm._CONTAINER_NAME]
        ), "Expected createAndPromote in exec history"

        validate_container(ctx, state_out)

    def test_initial_with_invalid_config(
        self, ctx: testing.Context, active_state: testing.State, populated_config: dict
    ) -> None:
        """Test that reconciliation runs successfully with an invalid config."""
        invalid_config = dict(populated_config)
        invalid_config["composer"] = "invalid-json"

        state_in = dataclasses.replace(active_state, config=invalid_config)
        with (
            ctx(ctx.on.update_status(), state_in) as mgr,
            pytest.raises(CharmConfigInvalidError, match="Invalid charm configuration"),
        ):
            mgr.charm.mediawiki.reconciliation(MediaWikiSecrets.generate())

    def test_initial_not_leader(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        meta: dict,
        mock_database_cursor: MockType,
    ) -> None:
        """Test that install commands do not run when not a leader unit, and the leader is allowed to do the installation."""
        mock_database_cursor.fetchone.side_effect = [None, None, "mocked-return"]

        state_in = dataclasses.replace(active_state, leader=False)
        with ctx(ctx.on.update_status(), state_in) as mgr:
            mgr.charm.mediawiki.reconciliation(MediaWikiSecrets.generate())

            state_out = mgr.run()

        assert ctx.exec_history == {}, "Did not expect any commands to be executed"

        validate_container(ctx, state_out, meta=meta)

    def test_read_only_database(
        self, ctx: testing.Context, active_state: testing.State, meta: dict
    ) -> None:
        """Test that reconciliation can run successfully with a read-only database."""
        with ctx(ctx.on.update_status(), active_state) as mgr:
            mgr.charm.mediawiki.reconciliation(MediaWikiSecrets.generate(), ro_database=True)

            state_out = mgr.run()

        validate_container(ctx, state_out, meta=meta, expect_read_only_db=True)

    def test_initial_with_valid_proxy(
        self,
        ctx: testing.Context,
        configured_state: testing.State,
        monkeypatch: pytest.MonkeyPatch,
    ):
        proxy_host = "http://proxy.internal"
        proxy_port = "3128"
        no_proxy = "127.0.0.1,::1"

        proxy_url = f"{proxy_host}:{proxy_port}"
        monkeypatch.setenv("JUJU_CHARM_HTTP_PROXY", proxy_url)
        monkeypatch.setenv("JUJU_CHARM_HTTPS_PROXY", proxy_url)
        monkeypatch.setenv("JUJU_CHARM_NO_PROXY", no_proxy)

        with ctx(ctx.on.update_status(), configured_state) as mgr:
            mgr.charm.mediawiki.reconciliation(MediaWikiSecrets.generate())

        found_one = False
        for exec_event in ctx.exec_history[Charm._CONTAINER_NAME]:
            if "/usr/bin/composer" not in exec_event.command:
                continue
            found_one = True

            env = exec_event.environment
            assert env.get("HTTP_PROXY") == proxy_url, (
                "HTTP_PROXY was not set correctly in exec env"
            )
            assert env.get("HTTPS_PROXY") == proxy_url, (
                "HTTPS_PROXY was not set correctly in exec env"
            )
            assert env.get("NO_PROXY") == no_proxy, "NO_PROXY was not set correctly in exec env"

        assert found_one, "Expected at least one composer command in exec history"

    def test_webroot_owner_ssh_key_generation(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_container: scenario.Container,
    ) -> None:
        """Test that the SSH key generation command is run during reconciliation if it hasn't been generated yet."""
        container_no_ssh = dataclasses.replace(
            mediawiki_container,
            mounts={k: v for k, v in mediawiki_container.mounts.items() if k != "ssh_dir"},
        )
        state_in = dataclasses.replace(active_state, containers=[container_no_ssh])

        with ctx(ctx.on.update_status(), state_in) as mgr:
            mgr.charm.mediawiki.reconciliation(MediaWikiSecrets.generate())

        assert any(
            ExecCmd.SSH_KEYGEN.value[0] in cmd.command
            for cmd in ctx.exec_history[Charm._CONTAINER_NAME]
        ), "Expected ssh-keygen to be called when SSH key does not exist"


class TestRotateRootCredentials:
    def test_success(self, ctx: testing.Context, active_state: testing.State) -> None:
        """Test that we get a correct return when createAndPromote is successful."""
        with ctx(ctx.on.update_status(), active_state) as mgr:
            user, password = mgr.charm.mediawiki.rotate_root_credentials()

            assert user == MediaWiki._ROOT_USER_NAME
            assert isinstance(password, str) and len(password) >= 64

    def test_failure(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_container: scenario.Container,
    ) -> None:
        """Test that we get a correct exception when createAndPromote fails."""
        execs = {
            testing.Exec(
                ExecCmd.MAINTENANCE_CREATE_AND_PROMOTE.value,
                return_code=1,
                stdout="",
                stderr="Mocked failure of createAndPromote",
            )
        }

        mediawiki_container = dataclasses.replace(mediawiki_container, execs=execs)
        state_in = dataclasses.replace(active_state, containers=[mediawiki_container])
        with (
            ctx(ctx.on.update_status(), state_in) as mgr,
            pytest.raises(MediaWikiInstallError, match="Creating root user failed"),
        ):
            mgr.charm.mediawiki.rotate_root_credentials()


class TestUpdateDatabaseScheme:
    def test_success(self, ctx: testing.Context, active_state: testing.State) -> None:
        """Test that the database update method runs without error."""
        with ctx(ctx.on.update_status(), active_state) as mgr:
            mgr.charm.mediawiki.update_database_schema()

    def test_failure(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_container: scenario.Container,
    ) -> None:
        """Test that we get a correct exception when update-database fails."""
        execs = {
            testing.Exec(
                ExecCmd.MAINTENANCE_UPDATE.value,
                return_code=1,
                stdout="",
                stderr="Mocked failure of update-database",
            )
        }

        mediawiki_container = dataclasses.replace(mediawiki_container, execs=execs)
        state_in = dataclasses.replace(active_state, containers=[mediawiki_container])
        with (
            ctx(ctx.on.update_status(), state_in) as mgr,
            pytest.raises(MediaWikiInstallError, match="Database schema update failed"),
        ):
            mgr.charm.mediawiki.update_database_schema()


class TestGetVersion:
    def test_success(
        self, ctx: testing.Context, active_state: testing.State, mocker: MockerFixture
    ) -> None:
        """Test that we can get a version string successfully."""
        mock_response = mocker.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"query": {"general": {"generator": "Mocked Version"}}}
        mock_get = mocker.patch("mediawiki.requests.get", return_value=mock_response)

        with ctx(ctx.on.update_status(), active_state) as mgr:
            assert mgr.charm.mediawiki.get_version() == "mocked-version", (
                "Did not get expected version string"
            )

            mock_get.assert_called_once()

    def test_request_failure(
        self, ctx: testing.Context, active_state: testing.State, mocker: MockerFixture
    ) -> None:
        """Test that we get an exception when the version request fails."""
        mock_response = mocker.Mock()
        mock_response.status_code = 500
        mock_get = mocker.patch("mediawiki.requests.get", return_value=mock_response)

        with ctx(ctx.on.update_status(), active_state) as mgr:
            assert mgr.charm.mediawiki.get_version() == "", (
                "Did not expect a version string on request failure"
            )

            mock_get.assert_called_once()

    def test_not_json_response(
        self, ctx: testing.Context, active_state: testing.State, mocker: MockerFixture
    ) -> None:
        """Test that we handle an unexpected response format gracefully."""
        mock_response = mocker.Mock()
        mock_response.status_code = 200
        mock_response.json.side_effect = requests.exceptions.JSONDecodeError(
            "Mocked JSON decode error", "", 0
        )
        mock_get = mocker.patch("mediawiki.requests.get", return_value=mock_response)

        with ctx(ctx.on.update_status(), active_state) as mgr:
            assert mgr.charm.mediawiki.get_version() == "", (
                "Did not expect a version string on JSON decode failure"
            )

            mock_get.assert_called_once()


class TestMediaWikiSecrets:
    def test_generate_returns_instance(self) -> None:
        """Test that generate() returns a MediaWikiSecrets instance."""
        result = MediaWikiSecrets.generate()
        assert isinstance(result, MediaWikiSecrets)

    def test_generate_fields_are_nonempty_strings(self) -> None:
        """Test that generated secrets are non-empty strings."""
        result = MediaWikiSecrets.generate()
        assert isinstance(result.secret_key, str) and len(result.secret_key) > 0
        assert isinstance(result.session_secret, str) and len(result.session_secret) > 0

    def test_generate_fields_have_sufficient_entropy(self) -> None:
        """Test that generated secrets are long enough to be considered secure."""
        result = MediaWikiSecrets.generate()
        # token_urlsafe(64) produces at least 64 bytes of entropy
        assert len(result.secret_key) >= 64
        assert len(result.session_secret) >= 64

    def test_generate_fields_differ_from_each_other(self) -> None:
        """Test that secret_key and session_secret are not the same value."""
        result = MediaWikiSecrets.generate()
        assert result.secret_key != result.session_secret

    def test_generate_produces_unique_secrets_on_each_call(self) -> None:
        """Test that two calls to generate() return different values."""
        first = MediaWikiSecrets.generate()
        second = MediaWikiSecrets.generate()
        assert first.secret_key != second.secret_key
        assert first.session_secret != second.session_secret

    def test_to_local_settings_values_match_fields(self) -> None:
        """Test that to_local_settings() values correspond to the dataclass fields."""
        result = MediaWikiSecrets(secret_key="test-key", session_secret="test-session")  # nosec: B106
        settings = result.to_local_settings()
        assert settings["$wgSecretKey"] == "test-key"
        assert settings["$wgSessionSecret"] == "test-session"


def validate_container(
    ctx: testing.Context,
    state_out: testing.State,
    expect_composer: bool = False,
    meta: dict | None = None,
    expect_read_only_db: bool = False,
) -> None:
    """Helper function to validate the container state.

    Args:
        state_out: The state output from the charm.
        expect_composer: Whether the composer.user.json file is expected to be present.
    """
    if meta is None:
        meta = {}

    container_fs = state_out.get_container(Charm._CONTAINER_NAME).get_filesystem(ctx)

    # Octal mode overrides for files that differ from the default 0o640.
    mode_overrides = {"home/webroot_owner/.ssh/config": 0o600}
    files = [
        "var/www/html/robots.txt",
        "var/www/html/w/LocalSettings.php",
        "etc/mediawiki/UserSettings.php",
        "etc/mediawiki/LateSettings.php",
        "home/webroot_owner/.ssh/config",
    ]

    if expect_composer:
        files.append("var/www/html/w/composer.user.json")

    for file in files:
        assert (container_fs / file).exists(), f"{file} does not exist in container filesystem"
        assert (container_fs / file).stat().st_mode & 0o777 == mode_overrides.get(file, 0o640), (
            f"{file} does not have correct permissions"
        )

    # Revert to metadata defaults for config values if not provided in state_out
    config = meta.get("config", {}).get("options", {})
    assert (container_fs / "var/www/html/robots.txt").read_text() == state_out.config.get(
        "robots-txt", config.get("robots-txt", {}).get("default", "")
    ), "robots.txt content does not match config"
    assert (container_fs / "etc/mediawiki/UserSettings.php").read_text() == state_out.config.get(
        "local-settings", config.get("local-settings", {}).get("default", "")
    ), "UserSettings.php content does not match config"

    ro_db_settings = [
        "$adminTask = ( PHP_SAPI === 'cli' || defined( 'MEDIAWIKI_INSTALL' ) );",
        "$wgReadOnly = $adminTask ? false : 'Ongoing database update';",
    ]

    if expect_read_only_db:
        for setting in ro_db_settings:
            assert setting in (container_fs / "etc/mediawiki/LateSettings.php").read_text(), (
                f"Expected read-only database setting not found in LateSettings.php: {setting}"
            )
    else:
        late_settings_content = (container_fs / "etc/mediawiki/LateSettings.php").read_text()
        for setting in ro_db_settings:
            assert setting not in late_settings_content, (
                f"Did not expect read-only database setting in LateSettings.php: {setting}"
            )
