# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.


import dataclasses
import json

import pytest
from ops import testing
from pytest_mock import MockerFixture, MockType

import database
import oauth
import s3
from charm import Charm
from exceptions import MediaWikiBlockedStatusException, MediaWikiInstallError
from mediawiki import MediaWiki, MediaWikiSecrets
from state import CharmConfigInvalidError, StatefulCharmBase
from tests.unit.conftest import ExecCmd
from types_ import DatabaseConfig, DatabaseEndpoint, S3ConnectionInfo


class WrapperCharm(StatefulCharmBase):
    """A minimal wrapper charm class to build a testing context with."""

    def __init__(self, *args):
        super().__init__(*args)
        self.database = database.Database(self, "database", Charm._CONTAINER_NAME)
        self.oauth = oauth.OAuth(self, "oauth")
        self.s3 = s3.S3(self, "s3-parameters")
        self.mediawiki = MediaWiki(self, self.database, self.oauth, self.s3)


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
def mock_oauth(mocker: MockerFixture) -> MockType:
    """Base OAuth class mock.

    By default, makes it so OAuth does nothing.
    """
    mock_oauth_cls = mocker.patch("oauth.OAuth", autospec=True)
    mock_instance = mock_oauth_cls.return_value

    mock_instance.update_client_config.return_value = None
    mock_instance.get_provider_info.return_value = None

    return mock_instance


@pytest.fixture(autouse=True)
def mock_s3(mocker: MockerFixture) -> MockType:
    """Base s3 class mock."""
    mock_s3_cls = mocker.patch("s3.S3", autospec=True)
    mock_instance = mock_s3_cls.return_value

    mock_instance.get_relation_data.return_value = S3ConnectionInfo.model_validate(
        {
            "endpoint": "mocked-s3-endpoint:9000",
            "access-key": "mocked-access-key",
            "secret-key": "mocked-secret-key",  # nosec: B106
            "bucket": "mocked-bucket",
        }
    )
    mock_instance.has_relation.return_value = True

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

        history = ctx.exec_history[Charm._CONTAINER_NAME]
        for cmd in [
            ExecCmd.MAINTENANCE_INSTALL_PRE_CONFIGURED,
            ExecCmd.MAINTENANCE_UPDATE,
            ExecCmd.MAINTENANCE_CREATE_AND_PROMOTE,
        ]:
            assert cmd.ran_in(history), f"{cmd.name} not found in exec history"

        validate_container(ctx, state_out, meta=meta)

    def test_initial_with_valid_config(
        self, ctx: testing.Context, configured_state: testing.State
    ) -> None:
        """Test that reconciliation runs successfully with a valid config."""
        with ctx(ctx.on.update_status(), configured_state) as mgr:
            mgr.charm.mediawiki.reconciliation(MediaWikiSecrets.generate())

            state_out = mgr.run()

        history = ctx.exec_history[Charm._CONTAINER_NAME]
        for cmd in [
            ExecCmd.COMPOSER_UPDATE,
            ExecCmd.MAINTENANCE_INSTALL_PRE_CONFIGURED,
            ExecCmd.MAINTENANCE_UPDATE,
            ExecCmd.MAINTENANCE_CREATE_AND_PROMOTE,
        ]:
            assert cmd.ran_in(history), f"{cmd.name} not found in exec history"

        validate_container(ctx, state_out, expect_composer=True)

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

    def test_ssh_key_written_when_provided(
        self,
        ctx: testing.Context,
        active_state: testing.State,
    ) -> None:
        """Test that a user-provided SSH key is written to id_charm in the container."""
        fake_key = "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----\n"
        with ctx(ctx.on.update_status(), active_state) as mgr:
            mgr.charm.mediawiki.reconciliation(MediaWikiSecrets.generate(), ssh_key=fake_key)
            state_out = mgr.run()

        container_fs = state_out.get_container(Charm._CONTAINER_NAME).get_filesystem(ctx)
        key_path = container_fs / "home/webroot_owner/.ssh/id_charm"
        assert key_path.exists(), "id_charm key file was not written"
        assert key_path.read_text() == fake_key
        assert key_path.stat().st_mode & 0o777 == 0o600, "id_charm must be 0o600"

        config_text = (container_fs / "home/webroot_owner/.ssh/config").read_text()
        assert "IdentityFile" in config_text, "Expected IdentityFile in SSH config when key is set"

    def test_composer_update_failure(
        self,
        ctx: testing.Context,
        configured_state: testing.State,
        mediawiki_container: testing.Container,
    ) -> None:
        """Test that when composer update fails, composer.user.json exists but does not match the config."""
        execs = {
            testing.Exec(
                ExecCmd.COMPOSER_UPDATE.value,
                return_code=1,
                stdout="",
                stderr="Mocked composer update failure",
            )
        }
        mediawiki_container = dataclasses.replace(mediawiki_container, execs=execs)
        state_in = dataclasses.replace(configured_state, containers=[mediawiki_container])

        with (
            ctx(ctx.on.update_status(), state_in) as mgr,
            pytest.raises(Exception, match="Composer update failed"),
        ):
            mgr.charm.mediawiki.reconciliation(MediaWikiSecrets.generate())

        container_fs = state_in.get_container(Charm._CONTAINER_NAME).get_filesystem(ctx)
        composer_file = container_fs / "var/www/html/w/composer.user.json"
        assert composer_file.exists(), "composer.user.json should exist even after a failed update"
        assert composer_file.stat().st_mode & 0o777 == 0o640, "composer.user.json must be 0o640"

        on_disk = json.loads(composer_file.read_text())
        config_composer = state_in.config.get("composer", {})
        if isinstance(config_composer, str):
            config_composer = json.loads(config_composer)
        assert on_disk != config_composer, (
            "composer.user.json should not match config after a failed update"
        )

    def test_ssh_key_not_written_when_absent(
        self,
        ctx: testing.Context,
        active_state: testing.State,
    ) -> None:
        """Test that no id_charm file is written when no SSH key is provided."""
        with ctx(ctx.on.update_status(), active_state) as mgr:
            mgr.charm.mediawiki.reconciliation(MediaWikiSecrets.generate())
            state_out = mgr.run()

        container_fs = state_out.get_container(Charm._CONTAINER_NAME).get_filesystem(ctx)
        assert not (container_fs / "home/webroot_owner/.ssh/id_charm").exists(), (
            "id_charm should not be present when no SSH key is provided"
        )

        config_text = (container_fs / "home/webroot_owner/.ssh/config").read_text()
        assert "IdentityFile" not in config_text, "Did not expect IdentityFile without an SSH key"


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
        mediawiki_container: testing.Container,
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
        mediawiki_container: testing.Container,
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


class TestS3Settings:
    def test_no_s3_relation_disables_uploads(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mock_s3: MockType,
    ) -> None:
        """Test that uploads are disabled when no S3 relation exists."""
        mock_s3.has_relation.return_value = False

        with ctx(ctx.on.update_status(), active_state) as mgr:
            mgr.charm.mediawiki.reconciliation(MediaWikiSecrets.generate())
            state_out = mgr.run()

        late_settings = (
            state_out.get_container(Charm._CONTAINER_NAME).get_filesystem(ctx)
            / "etc/mediawiki/LateSettings.php"
        ).read_text()

        assert "$wgEnableUploads = false;" in late_settings
        assert "wfLoadExtension( 'AWS' );" not in late_settings

    def test_s3_relation_loads_aws_extension_with_credentials(
        self,
        ctx: testing.Context,
        active_state: testing.State,
    ) -> None:
        """Test that the AWS extension and credentials are rendered in LateSettings.php when S3 is configured."""
        with ctx(ctx.on.update_status(), active_state) as mgr:
            mgr.charm.mediawiki.reconciliation(MediaWikiSecrets.generate())
            state_out = mgr.run()

        late_settings = (
            state_out.get_container(Charm._CONTAINER_NAME).get_filesystem(ctx)
            / "etc/mediawiki/LateSettings.php"
        ).read_text()

        assert "wfLoadExtension( 'AWS' );" in late_settings
        assert "'key' => 'mocked-access-key'" in late_settings
        assert "'secret' => 'mocked-secret-key'" in late_settings
        assert "$wgAWSBucketName = 'mocked-bucket'" in late_settings
        assert "$wgFileBackends['s3']['endpoint'] = 'mocked-s3-endpoint:9000'" in late_settings
        assert "$wgEnableUploads = false;" not in late_settings

    def test_s3_defaults_to_eu_west_1_region_when_none_set(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mock_s3: MockType,
    ) -> None:
        """Test that eu-west-1 is used as the default region when none is specified in the S3 relation data."""
        mock_s3.get_relation_data.return_value = S3ConnectionInfo.model_validate(
            {
                "endpoint": "mocked-s3-endpoint:9000",
                "access-key": "mocked-access-key",
                "secret-key": "mocked-secret-key",  # nosec: B106
                "bucket": "mocked-bucket",
            }
        )

        with ctx(ctx.on.update_status(), active_state) as mgr:
            mgr.charm.mediawiki.reconciliation(MediaWikiSecrets.generate())
            state_out = mgr.run()

        late_settings = (
            state_out.get_container(Charm._CONTAINER_NAME).get_filesystem(ctx)
            / "etc/mediawiki/LateSettings.php"
        ).read_text()

        assert "$wgAWSRegion = 'eu-west-1'" in late_settings

    def test_s3_custom_region_is_used(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mock_s3: MockType,
    ) -> None:
        """Test that a custom region from the S3 relation data is written to LateSettings.php."""
        mock_s3.get_relation_data.return_value = S3ConnectionInfo.model_validate(
            {
                "endpoint": "mocked-s3-endpoint:9000",
                "access-key": "mocked-access-key",
                "secret-key": "mocked-secret-key",  # nosec: B106
                "bucket": "mocked-bucket",
                "region": "us-east-1",
            }
        )

        with ctx(ctx.on.update_status(), active_state) as mgr:
            mgr.charm.mediawiki.reconciliation(MediaWikiSecrets.generate())
            state_out = mgr.run()

        late_settings = (
            state_out.get_container(Charm._CONTAINER_NAME).get_filesystem(ctx)
            / "etc/mediawiki/LateSettings.php"
        ).read_text()

        assert "$wgAWSRegion = 'us-east-1'" in late_settings
        assert "$wgAWSRegion = 'eu-west-1'" not in late_settings

    def test_s3_path_style_endpoint_is_set(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mock_s3: MockType,
    ) -> None:
        """Test that path-style endpoint is configured in LateSettings.php when s3-uri-style is 'path'."""
        mock_s3.get_relation_data.return_value = S3ConnectionInfo.model_validate(
            {
                "endpoint": "mocked-s3-endpoint:9000",
                "access-key": "mocked-access-key",
                "secret-key": "mocked-secret-key",  # nosec: B106
                "bucket": "mocked-bucket",
                "s3-uri-style": "path",
            }
        )

        with ctx(ctx.on.update_status(), active_state) as mgr:
            mgr.charm.mediawiki.reconciliation(MediaWikiSecrets.generate())
            state_out = mgr.run()

        late_settings = (
            state_out.get_container(Charm._CONTAINER_NAME).get_filesystem(ctx)
            / "etc/mediawiki/LateSettings.php"
        ).read_text()

        assert "$wgFileBackends['s3']['use_path_style_endpoint'] = true;" in late_settings

    def test_s3_host_style_does_not_set_path_style_endpoint(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mock_s3: MockType,
    ) -> None:
        """Test that use_path_style_endpoint is not set when s3-uri-style is 'host'."""
        mock_s3.get_relation_data.return_value = S3ConnectionInfo.model_validate(
            {
                "endpoint": "mocked-s3-endpoint:9000",
                "access-key": "mocked-access-key",
                "secret-key": "mocked-secret-key",  # nosec: B106
                "bucket": "mocked-bucket",
                "s3-uri-style": "host",
            }
        )

        with ctx(ctx.on.update_status(), active_state) as mgr:
            mgr.charm.mediawiki.reconciliation(MediaWikiSecrets.generate())
            state_out = mgr.run()

        late_settings = (
            state_out.get_container(Charm._CONTAINER_NAME).get_filesystem(ctx)
            / "etc/mediawiki/LateSettings.php"
        ).read_text()

        assert "use_path_style_endpoint" not in late_settings

    def test_incomplete_s3_relation_data_disables_uploads(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mock_s3: MockType,
    ) -> None:
        """Test that uploads are disabled when S3 relation data is incomplete, and a block is raised."""
        # Mock get relation data to raise block status
        mock_s3.get_relation_data.side_effect = MediaWikiBlockedStatusException(
            "Mocked block status"
        )

        with ctx(ctx.on.update_status(), active_state) as mgr:
            with pytest.raises(MediaWikiBlockedStatusException, match="Mocked block status"):
                mgr.charm.mediawiki.reconciliation(MediaWikiSecrets.generate())
            state_out = mgr.run()

        late_settings = (
            state_out.get_container(Charm._CONTAINER_NAME).get_filesystem(ctx)
            / "etc/mediawiki/LateSettings.php"
        ).read_text()

        assert "$wgEnableUploads = false;" in late_settings
        assert "wfLoadExtension( 'AWS' );" not in late_settings


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

    try:
        composer_content = (container_fs / "var/www/html/w/composer.user.json").read_text()
    except FileNotFoundError:
        composer_content = "{}"
    raw_composer = state_out.config.get("composer", "{}")
    assert isinstance(raw_composer, str), "Expected composer config to be a string"
    expected_composer = json.loads(raw_composer) if raw_composer else {}
    assert json.loads(composer_content) == expected_composer, (
        "composer.user.json content does not match config"
    )
