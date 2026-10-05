# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for the Shellbox manager and its rock image configuration."""

import configparser
import dataclasses
from pathlib import Path

import ops
import pytest
from charmlibs.pathops import ensure_contents
from ops import pebble, testing
from pytest_mock import MockerFixture, MockType

import utils
from shellbox import SHELLBOX_URL, Shellbox


@pytest.fixture(autouse=True)
def mock_mediawiki(mocker: MockerFixture) -> MockType:
    """Keep charm-level Shellbox tests independent of MediaWiki reconciliation."""
    mediawiki = mocker.patch("charm.MediaWiki", autospec=True).return_value
    mediawiki.reconciliation.return_value = False
    mediawiki.version.return_value = "1.46.0"
    return mediawiki


@pytest.fixture(autouse=True)
def mock_git_sync(mocker: MockerFixture) -> MockType:
    """Keep charm-level Shellbox tests independent of git-sync reconciliation."""
    git_sync = mocker.patch("charm.GitSync", autospec=True).return_value
    git_sync.is_ready.return_value = True
    git_sync.metrics_scrape_jobs.return_value = []
    return git_sync


class TestShellbox:
    """Shellbox sidecar configuration, prerequisites, and unit authentication."""

    def test_pebble_ready_configures_authenticated_sidecar(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        shellbox_container: testing.Container,
        mock_mediawiki: MockType,
    ) -> None:
        """Start the rock services with a generic unit secret for the per-unit key."""
        state_out = ctx.run(ctx.on.pebble_ready(shellbox_container), active_state)

        assert isinstance(state_out.unit_status, ops.ActiveStatus)
        secret = state_out.get_secret(label=utils.UNIT_SECRET_LABEL)
        assert secret.owner == "unit"
        key = secret.latest_content[Shellbox.AUTHENTICATION_KEY_CONTENT_KEY]
        assert len(key) == 128
        container = state_out.get_container("shellbox")
        assert all(service.startup == "enabled" for service in container.plan.services.values())
        assert all(
            status == pebble.ServiceStatus.ACTIVE for status in container.service_statuses.values()
        )
        assert container.plan.checks["shellbox"].http == {"url": f"{SHELLBOX_URL}/healthz"}
        key_file = container.get_filesystem(ctx) / Shellbox.KEY_FILE.lstrip("/")
        assert key_file.read_text() == f"[shellbox]\nenv[SHELLBOX_SECRET_KEY] = {key}\n"
        assert key_file.stat().st_mode & 0o777 == 0o600
        # Pebble serves its plan to every user in the container.
        assert all(
            "SHELLBOX_SECRET_KEY" not in service.environment
            for service in container.plan.services.values()
        )
        mock_mediawiki.reconciliation.assert_called_once_with(ssh_key=None, force=False)

    def test_php_fpm_restarts_only_when_key_file_changes(
        self, ctx: testing.Context, active_state: testing.State, mocker: MockerFixture
    ) -> None:
        """Restart PHP-FPM to load a rewritten key, and leave it running otherwise."""
        # The testing filesystem reports the local user as every file's owner.
        mocker.patch(
            "shellbox.ensure_contents",
            side_effect=lambda path, source, mode, **_: ensure_contents(path, source, mode=mode),
        )
        restart = mocker.spy(ops.Container, "restart")
        with ctx(ctx.on.update_status(), active_state) as manager:
            manager.charm._shellbox.reconciliation()
            restart.assert_called_once_with(mocker.ANY, "php-fpm")

            restart.reset_mock()
            manager.charm._shellbox.reconciliation()
            restart.assert_not_called()

    def test_key_survives_reconciliation(
        self, ctx: testing.Context, active_state: testing.State
    ) -> None:
        """Reuse the unit key when reconciliation runs again."""
        state_out = ctx.run(ctx.on.config_changed(), active_state)
        key = state_out.get_secret(label=utils.UNIT_SECRET_LABEL).latest_content[
            Shellbox.AUTHENTICATION_KEY_CONTENT_KEY
        ]
        reconciled = ctx.run(ctx.on.config_changed(), state_out)
        assert (
            reconciled.get_secret(label=utils.UNIT_SECRET_LABEL).latest_content[
                Shellbox.AUTHENTICATION_KEY_CONTENT_KEY
            ]
            == key
        )

    def test_unit_secret_holds_multiple_named_values(
        self, ctx: testing.Context, active_state: testing.State
    ) -> None:
        """Add future unit values to the same generic secret without replacing existing ones."""
        with ctx(ctx.on.update_status(), active_state) as manager:
            key = manager.charm._shellbox.authentication_key()
            additional_value = utils.unit_secret_value(
                manager.charm, "another-value", lambda: "another-secret"
            )
            secret = manager.charm.model.get_secret(label=utils.UNIT_SECRET_LABEL)
            secret_content = secret.get_content(refresh=True)

        assert additional_value == "another-secret"
        assert secret_content == {
            Shellbox.AUTHENTICATION_KEY_CONTENT_KEY: key,
            "another-value": "another-secret",
        }

    def test_waits_for_sidecar(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        shellbox_container: testing.Container,
        mock_mediawiki: MockType,
    ) -> None:
        """Do not reconcile MediaWiki before its command execution sidecar is ready."""
        disconnected = dataclasses.replace(shellbox_container, can_connect=False)
        state_in = dataclasses.replace(
            active_state,
            containers=[
                disconnected,
                *(
                    container
                    for container in active_state.containers
                    if container.name != "shellbox"
                ),
            ],
        )
        state_out = ctx.run(ctx.on.config_changed(), state_in)
        assert state_out.unit_status == ops.WaitingStatus("Waiting for shellbox sidecar")
        mock_mediawiki.reconciliation.assert_not_called()
        assert all(secret.label != utils.UNIT_SECRET_LABEL for secret in state_out.secrets)

    def test_server_does_not_require_mediawiki(
        self, ctx: testing.Context, active_state: testing.State
    ) -> None:
        """Configure the server without accessing a disconnected MediaWiki container."""
        state_in = dataclasses.replace(
            active_state,
            containers=[
                dataclasses.replace(container, can_connect=False)
                if container.name == "mediawiki"
                else container
                for container in active_state.containers
            ],
        )
        with ctx(ctx.on.update_status(), state_in) as manager:
            manager.charm._shellbox.reconciliation()
            key = manager.charm._shellbox.authentication_key()
            container = manager.charm.unit.get_container("shellbox")
            assert container.pull(Shellbox.KEY_FILE).read() == (
                f"[shellbox]\nenv[SHELLBOX_SECRET_KEY] = {key}\n"
            )
            assert container.get_plan().checks["shellbox"].http == {
                "url": f"{SHELLBOX_URL}/healthz"
            }
            content = manager.charm.model.get_secret(label=utils.UNIT_SECRET_LABEL).get_content()
            assert content[Shellbox.AUTHENTICATION_KEY_CONTENT_KEY] == key

    def test_mediawiki_rotation_preserves_shellbox_key(
        self,
        ctx: testing.Context,
        active_state: testing.State,
    ) -> None:
        """Keep the unit key stable when shared MediaWiki secrets change."""
        configured = ctx.run(ctx.on.config_changed(), active_state)
        key = configured.get_secret(label=utils.UNIT_SECRET_LABEL).latest_content[
            Shellbox.AUTHENTICATION_KEY_CONTENT_KEY
        ]
        rotated = ctx.run(ctx.on.action("rotate-mediawiki-secrets"), configured)
        reconciled = ctx.run(ctx.on.config_changed(), rotated)
        assert (
            reconciled.get_secret(label=utils.UNIT_SECRET_LABEL).latest_content[
                Shellbox.AUTHENTICATION_KEY_CONTENT_KEY
            ]
            == key
        )
        assert reconciled.unit_status == ops.ActiveStatus()

    def test_health_check_tolerates_saturated_workers(
        self, ctx: testing.Context, active_state: testing.State
    ) -> None:
        """Fail the check only after the longest command PHP-FPM allows could have finished."""
        pool = configparser.ConfigParser()
        pool.read(Path(__file__).parents[2] / "shellbox_rock/files/etc/shellbox/pool.conf")
        request_timeout = int(pool["shellbox"]["request_terminate_timeout"].removesuffix("s"))
        state_out = ctx.run(ctx.on.config_changed(), active_state)
        check = state_out.get_container("shellbox").plan.checks["shellbox"]
        period = int(check.period.removesuffix("s"))
        assert period * check.threshold > request_timeout
