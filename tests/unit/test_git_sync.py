# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import dataclasses

import pytest
from ops import testing
from pytest_mock import MockerFixture, MockType

from exceptions import MediaWikiBlockedStatusException
from git_sync import GitSync
from state import StatefulCharmBase
from tests.unit.conftest import MOCK_SSH_KEY


class WrapperCharm(StatefulCharmBase):
    """A minimal wrapper charm class to build a testing context with."""

    def __init__(self, *args):
        super().__init__(*args)
        self.git_sync = GitSync(self)


@pytest.fixture(autouse=True)
def mock_ssh_reconcile_config(mocker: MockerFixture) -> MockType:
    """Mock ssh_reconcile_config since it is tested separately."""
    return mocker.patch("utils.ssh_reconcile_config")


@pytest.fixture
def ctx(meta: dict) -> testing.Context:
    """Provide a Context with the WrapperCharm."""
    m = meta.copy()
    config = m.pop("config", None)
    actions = m.pop("actions", None)
    return testing.Context(WrapperCharm, meta=m, config=config, actions=actions)


_KNOWN_HOSTS = "github.com ssh-rsa AAAAB3...\n"
_GIT_REPO = "git@github.com:user/repo.git"


@pytest.fixture
def configured_git_sync_state(active_state: testing.State) -> testing.State:
    """Provide an active state preconfigured with git repo and known hosts."""
    return dataclasses.replace(
        active_state,
        config={
            **active_state.config,
            "ssh-known-hosts": _KNOWN_HOSTS,
            "static-assets-git-repo": _GIT_REPO,
        },
    )


class TestIsReady:
    """Tests for GitSync.is_ready()."""

    def test_ready(self, ctx: testing.Context, active_state: testing.State) -> None:
        """Test is_ready returns True when container, storage, and mount are all present."""
        with ctx(ctx.on.update_status(), active_state) as mgr:
            assert mgr.charm.git_sync.is_ready()

    def test_not_ready_cannot_connect(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        git_sync_container: testing.Container,
    ) -> None:
        """Test is_ready returns False when the container cannot connect."""
        disconnected = dataclasses.replace(git_sync_container, can_connect=False)
        state_in = dataclasses.replace(
            active_state,
            containers=[c for c in active_state.containers if c.name != "git-sync"]
            + [disconnected],
        )
        with ctx(ctx.on.update_status(), state_in) as mgr:
            assert not mgr.charm.git_sync.is_ready()

    def test_not_ready_no_storage(self, ctx: testing.Context, active_state: testing.State) -> None:
        """Test is_ready returns False when no storage is attached."""
        state_in = dataclasses.replace(active_state, storages=[])
        with ctx(ctx.on.update_status(), state_in) as mgr:
            assert not mgr.charm.git_sync.is_ready()


class TestReconciliation:
    """Tests for GitSync.reconciliation()."""

    def test_skips_when_not_ready(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mock_ssh_reconcile_config: MockType,
    ) -> None:
        """Test that reconciliation is skipped when the container is not ready."""
        state_in = dataclasses.replace(active_state, storages=[])
        with ctx(ctx.on.update_status(), state_in) as mgr:
            mgr.charm.git_sync.reconciliation(ssh_key=None)

        mock_ssh_reconcile_config.assert_not_called()

    def test_reconciliation_calls_ssh_config(
        self,
        ctx: testing.Context,
        configured_git_sync_state: testing.State,
        mock_ssh_reconcile_config: MockType,
    ) -> None:
        """Test that reconciliation calls ssh_reconcile_config."""
        with ctx(ctx.on.update_status(), configured_git_sync_state) as mgr:
            mgr.charm.git_sync.reconciliation(ssh_key=MOCK_SSH_KEY)

        mock_ssh_reconcile_config.assert_called_once()
        _, kwargs = mock_ssh_reconcile_config.call_args
        assert kwargs["ssh_key"] == MOCK_SSH_KEY

    def test_reconciliation_disables_on_ssh_failure(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mock_ssh_reconcile_config: MockType,
    ) -> None:
        """Test that when SSH config fails, the service is disabled and the error re-raised."""
        mock_ssh_reconcile_config.side_effect = MediaWikiBlockedStatusException("SSH failed")

        with (
            pytest.raises(MediaWikiBlockedStatusException, match="SSH failed"),
            ctx(ctx.on.update_status(), active_state) as mgr,
        ):
            mgr.charm.git_sync.reconciliation(ssh_key=None)

    def test_clears_repo_when_no_git_command(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        git_sync_mounts: dict,
    ) -> None:
        """Test that repo contents are cleared when no git repo is configured."""
        # Create a file in the mount to be "cleared"
        repo_source = git_sync_mounts["static_assets"].source
        (repo_source / "leftover.txt").write_text("old data")

        clear_exec = testing.Exec(
            ("find", "/mnt/static-assets", "-mindepth", "1", "-delete"),
            return_code=0,
        )
        git_sync_container = next(c for c in active_state.containers if c.name == "git-sync")
        git_sync_container = dataclasses.replace(git_sync_container, execs={clear_exec})
        state_in = dataclasses.replace(
            active_state,
            containers=[c for c in active_state.containers if c.name != "git-sync"]
            + [git_sync_container],
            config={**active_state.config, "static-assets-git-repo": ""},
        )

        with ctx(ctx.on.update_status(), state_in) as mgr:
            mgr.charm.git_sync.reconciliation(ssh_key=None)


class TestSshConfigReconciliation:
    """Tests for GitSync._ssh_config_reconciliation()."""

    def test_blocks_on_unknown_remote(
        self,
        ctx: testing.Context,
        active_state: testing.State,
    ) -> None:
        """Test that an SSH remote not in known_hosts raises BlockedStatusException."""
        state_in = dataclasses.replace(
            active_state,
            config={
                **active_state.config,
                "static-assets-git-repo": "git@gitlab.example.com:org/repo.git",
                "ssh-known-hosts": _KNOWN_HOSTS,
            },
        )
        with (
            pytest.raises(
                MediaWikiBlockedStatusException,
                match="not in the known hosts configuration",
            ),
            ctx(ctx.on.update_status(), state_in) as mgr,
        ):
            mgr.charm.git_sync._ssh_config_reconciliation(ssh_key=None)

    def test_no_error_for_known_remote(
        self,
        ctx: testing.Context,
        configured_git_sync_state: testing.State,
    ) -> None:
        """Test that a known SSH remote does not raise."""
        with ctx(ctx.on.update_status(), configured_git_sync_state) as mgr:
            mgr.charm.git_sync._ssh_config_reconciliation(ssh_key=None)

    def test_no_error_for_https_repo(
        self,
        ctx: testing.Context,
        active_state: testing.State,
    ) -> None:
        """Test that an HTTPS repo URL does not trigger known-hosts validation."""
        state_in = dataclasses.replace(
            active_state,
            config={
                **active_state.config,
                "static-assets-git-repo": "https://github.com/user/repo.git",
                "ssh-known-hosts": "",
            },
        )
        with ctx(ctx.on.update_status(), state_in) as mgr:
            mgr.charm.git_sync._ssh_config_reconciliation(ssh_key=None)


class TestPebbleLayer:
    """Tests for the Pebble layer construction."""

    def test_enabled_when_repo_configured(
        self,
        ctx: testing.Context,
        configured_git_sync_state: testing.State,
    ) -> None:
        """Test that the service is enabled when a git repo is configured."""
        with ctx(ctx.on.update_status(), configured_git_sync_state) as mgr:
            layer = mgr.charm.git_sync._pebble_layer()

        service = layer["services"]["git-sync"]
        assert service["startup"] == "enabled"
        assert _GIT_REPO in service["command"]

    def test_disabled_when_no_repo(
        self,
        ctx: testing.Context,
        active_state: testing.State,
    ) -> None:
        """Test that the service is disabled when no git repo is configured."""
        state_in = dataclasses.replace(
            active_state,
            config={**active_state.config, "static-assets-git-repo": ""},
        )
        with ctx(ctx.on.update_status(), state_in) as mgr:
            layer = mgr.charm.git_sync._pebble_layer()

        assert layer["services"]["git-sync"]["startup"] == "disabled"

    def test_force_disable(
        self,
        ctx: testing.Context,
        configured_git_sync_state: testing.State,
    ) -> None:
        """Test that force_disable overrides the startup to disabled."""
        with ctx(ctx.on.update_status(), configured_git_sync_state) as mgr:
            layer = mgr.charm.git_sync._pebble_layer(force_disable=True)

        assert layer["services"]["git-sync"]["startup"] == "disabled"

    def test_check_tracks_startup(
        self,
        ctx: testing.Context,
        configured_git_sync_state: testing.State,
    ) -> None:
        """Test that the health check startup matches the service startup."""
        with ctx(ctx.on.update_status(), configured_git_sync_state) as mgr:
            layer = mgr.charm.git_sync._pebble_layer()
            layer_disabled = mgr.charm.git_sync._pebble_layer(force_disable=True)

        assert layer["checks"]["git-sync-alive"]["startup"] == "enabled"
        assert layer_disabled["checks"]["git-sync-alive"]["startup"] == "disabled"


class TestGitSyncCommand:
    """Tests for the _git_sync_command property."""

    def test_empty_when_no_repo(
        self,
        ctx: testing.Context,
        active_state: testing.State,
    ) -> None:
        """Test that the command is empty when no git repo is configured."""
        state_in = dataclasses.replace(
            active_state,
            config={**active_state.config, "static-assets-git-repo": ""},
        )
        with ctx(ctx.on.update_status(), state_in) as mgr:
            assert mgr.charm.git_sync._git_sync_command == []

    def test_includes_repo_and_root(
        self,
        ctx: testing.Context,
        configured_git_sync_state: testing.State,
    ) -> None:
        """Test that the command includes the repo URL and root path."""
        with ctx(ctx.on.update_status(), configured_git_sync_state) as mgr:
            cmd = mgr.charm.git_sync._git_sync_command

        assert cmd[0] == "/git-sync"
        assert "--repo" in cmd
        assert _GIT_REPO in cmd
        assert "--root" in cmd
        assert "/mnt/static-assets" in cmd

    def test_includes_ref_when_configured(
        self,
        ctx: testing.Context,
        configured_git_sync_state: testing.State,
    ) -> None:
        """Test that --ref is included when a git ref is configured."""
        configured_git_sync_state = dataclasses.replace(
            configured_git_sync_state,
            config={
                **configured_git_sync_state.config,
                "static-assets-git-ref": "v1.0.0",
            },
        )

        with ctx(ctx.on.update_status(), configured_git_sync_state) as mgr:
            cmd = mgr.charm.git_sync._git_sync_command

        ref_idx = cmd.index("--ref")
        assert cmd[ref_idx + 1] == "v1.0.0"

    def test_no_ref_when_not_configured(
        self,
        ctx: testing.Context,
        configured_git_sync_state: testing.State,
    ) -> None:
        """Test that --ref is not present when git ref is empty."""
        state_in = dataclasses.replace(
            configured_git_sync_state,
            config={
                **configured_git_sync_state.config,
                "static-assets-git-ref": "",
            },
        )
        with ctx(ctx.on.update_status(), state_in) as mgr:
            cmd = mgr.charm.git_sync._git_sync_command

        assert "--ref" not in cmd

    def test_includes_sparse_checkout_file_when_present(
        self,
        ctx: testing.Context,
        active_state: testing.State,
    ) -> None:
        """Test that --sparse-checkout-file is included when the file has been written."""
        state_in = dataclasses.replace(
            active_state,
            config={
                **active_state.config,
                "static-assets-git-repo": _GIT_REPO,
                "ssh-known-hosts": _KNOWN_HOSTS,
            },
        )
        with ctx(ctx.on.update_status(), state_in) as mgr:
            mgr.charm.git_sync._sparse_checkout_file.write_text("asset.txt\n")
            cmd = mgr.charm.git_sync._git_sync_command

        assert "--sparse-checkout-file" in cmd
        idx = cmd.index("--sparse-checkout-file")
        assert "git-sync-sparse-checkout" in cmd[idx + 1]

    def test_no_sparse_checkout_when_file_absent(
        self,
        ctx: testing.Context,
        configured_git_sync_state: testing.State,
    ) -> None:
        """Test that --sparse-checkout-file is absent when the file does not exist."""
        with ctx(ctx.on.update_status(), configured_git_sync_state) as mgr:
            cmd = mgr.charm.git_sync._git_sync_command

        assert "--sparse-checkout-file" not in cmd


class TestSparseCheckoutReconciliation:
    """Tests for GitSync._sparse_checkout_reconciliation()."""

    def test_writes_file_when_configured(
        self,
        ctx: testing.Context,
        active_state: testing.State,
    ) -> None:
        """Test that the sparse checkout file is written when configured."""
        state_in = dataclasses.replace(
            active_state,
            config={
                **active_state.config,
                "static-assets-git-sparse-checkout": "asset.txt\n",
            },
        )
        with ctx(ctx.on.update_status(), state_in) as mgr:
            mgr.charm.git_sync._sparse_checkout_reconciliation()
            assert mgr.charm.git_sync._sparse_checkout_file.exists()

    def test_removes_file_when_cleared(
        self,
        ctx: testing.Context,
        active_state: testing.State,
    ) -> None:
        """Test that the sparse checkout file is removed when config is cleared."""
        state_in = dataclasses.replace(
            active_state,
            config={**active_state.config, "static-assets-git-sparse-checkout": ""},
        )
        with ctx(ctx.on.update_status(), state_in) as mgr:
            mgr.charm.git_sync._sparse_checkout_file.write_text("old content\n")
            assert mgr.charm.git_sync._sparse_checkout_file.exists()
            mgr.charm.git_sync._sparse_checkout_reconciliation()
            assert not mgr.charm.git_sync._sparse_checkout_file.exists()
