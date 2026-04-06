# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Module for managing and interacting with the git-sync sidecar container."""

import logging
from typing import Literal

from charmlibs.pathops import ContainerPath
from ops import ModelError, Object, pebble

import utils
from exceptions import MediaWikiBlockedStatusException
from state import StatefulCharmBase

logger = logging.getLogger(__name__)


class GitSync(Object):
    """Class to manage the git-sync sidecar container."""

    _sync_period = "10s"
    _max_sync_failures = 3

    _service_name = "git-sync"
    _git_sync_port = 8081
    _link = "repo"

    REPO_MOUNT_POINT = "/mnt/static-assets"

    def __init__(
        self,
        charm: StatefulCharmBase,
        container_name: str = "git-sync",
        storage_name: str = "static-assets-repo",
    ):
        """Initialize the GitSync manager.

        Args:
            charm: The charm instance.
            container_name: The name of the container running git-sync.
            storage_name: The name of the storage to use for git-sync.
        """
        super().__init__(charm, "git-sync-manager")
        self._charm = charm
        self._container = self.model.unit.get_container(container_name)
        self._storage_name = storage_name

        self._repo_mount_point = ContainerPath(self.REPO_MOUNT_POINT, container=self._container)
        self._link_target = self._repo_mount_point / self._link

        self._known_hosts_file = ContainerPath("/run/ssh_known_hosts", container=self._container)
        self._ssh_config_file = ContainerPath(
            "/etc/ssh/ssh_config.d/git-sync.conf", container=self._container
        )
        self._ssh_key_file = ContainerPath("/run/git-sync-ssh-key.priv", container=self._container)
        self._sparse_checkout_file = ContainerPath(
            "/run/git-sync-sparse-checkout", container=self._container
        )

    def reconciliation(self, ssh_key: str | None = None) -> None:
        """Reconcile the git-sync container configuration.

        This should be called during charm startup and whenever the SSH key or
        proxy configuration changes.

        Args:
            ssh_key: Optional SSH private key content to configure for git-sync.
                If None, any existing SSH key will be removed.
        """
        if not self.is_ready():
            logger.info("Skipping git-sync reconciliation, container is not ready")
            return

        try:
            self._ssh_config_reconciliation(ssh_key)
        except Exception:
            self._reconcile_services(force_disable=True)
            raise
        self._sparse_checkout_reconciliation()
        self._reconcile_services()

        if not self._git_sync_command:
            self._clear_repo_contents()

    def _clear_repo_contents(self) -> None:
        """Remove the link target."""
        mount = str(self._repo_mount_point)
        try:
            self._container.remove_path(self._link_target.as_posix(), recursive=True)
        except pebble.Error as e:
            logger.error("Failed to clear git-sync repo contents at %s: %s", mount, e)
            raise MediaWikiBlockedStatusException(
                f"Failed to clear git-sync repo contents: {e}"
            ) from e

        logger.info("Cleared static assets from %s.", mount)

    def is_ready(self) -> bool:
        """Check if the git-sync container is ready to be used."""
        if not self._container.can_connect():
            return False

        if len(self._charm.model.storages[self._storage_name]) == 0:
            return False

        try:
            _ = self._charm.model.storages[self._storage_name][0].location
        except ModelError:
            return False

        return self._repo_mount_point.exists()

    @property
    def _git_sync_command(self) -> list[str]:
        """Get the command to run git-sync with the current configuration."""
        config = self._charm.load_charm_config()
        if not config.static_assets_git_repo:
            return []

        # https://github.com/kubernetes/git-sync
        cmd = ["/git-sync"]
        cmd.extend(["--repo", config.static_assets_git_repo])
        cmd.extend(["--root", self._repo_mount_point.as_posix()])
        cmd.extend(["--period", self._sync_period])
        cmd.extend(["--max-failures", str(self._max_sync_failures)])
        cmd.extend(["--http-bind", f":{self._git_sync_port}"])
        cmd.extend(["--ssh-known-hosts-file", self._known_hosts_file.as_posix()])
        cmd.extend(["--link", self._link])

        if config.static_assets_git_ref:
            cmd.extend(["--ref", config.static_assets_git_ref])
        if self._ssh_key_file.exists():
            cmd.extend(["--ssh-key-file", self._ssh_key_file.as_posix()])
        if self._sparse_checkout_file.exists():
            cmd.extend(["--sparse-checkout-file", self._sparse_checkout_file.as_posix()])

        return cmd

    def _pebble_layer(self, *, force_disable: bool = False) -> pebble.LayerDict:
        """Build the Pebble layer for the git-sync service.

        Args:
            force_disable: Whether the service should be forcefully disabled. When True, the
                service is set to disabled so it won't start even if pebble restarts.
        """
        cmd = self._git_sync_command
        startup: Literal["enabled", "disabled"] = (
            "enabled" if cmd and not force_disable else "disabled"
        )

        if not cmd:
            cmd = ["sleep", "inf"]

        layer: pebble.LayerDict = {
            "summary": "git-sync layer",
            "description": "Pebble layer configuration for git-sync",
            "services": {
                self._service_name: {
                    "override": "replace",
                    "summary": "git-sync service",
                    "command": " ".join(cmd),
                    "startup": startup,
                    "on-check-failure": {"git-sync-alive": "restart"},
                    "environment": self._charm.state.proxy_config.as_dict
                    if self._charm.state.proxy_config
                    else {},
                },
            },
            "checks": {
                "git-sync-alive": {
                    "override": "replace",
                    "level": "ready",
                    "startup": startup,
                    "http": {"url": f"http://localhost:{self._git_sync_port}/"},
                    "period": "10s",
                    "timeout": "5s",
                }
            },
        }

        return layer

    def _reconcile_services(self, *, force_disable: bool = False) -> None:
        """Apply the Pebble layer and replan.

        Args:
            force_disable: Whether the git-sync service should be forcefully disabled.
        """
        self._container.add_layer(
            self._service_name, self._pebble_layer(force_disable=force_disable), combine=True
        )
        self._container.replan()

    def _sparse_checkout_reconciliation(self) -> None:
        """Write or remove the sparse-checkout file based on the current configuration."""
        config = self._charm.load_charm_config()
        if config.static_assets_git_sparse_checkout:
            self._sparse_checkout_file.write_text(config.static_assets_git_sparse_checkout)
        elif self._sparse_checkout_file.exists():
            self._container.remove_path(self._sparse_checkout_file.as_posix())

    def _ssh_config_reconciliation(self, ssh_key: str | None) -> None:
        """Configure the SSH environment for git-sync.

        Args:
            ssh_key: Optional SSH private key content to write into the container.

        Raises:
            MediaWikiBlockedStatusException: If the git remote is not in the known hosts configuration.
        """
        config = self._charm.load_charm_config()

        utils.ssh_reconcile_config(
            ssh_key=ssh_key,
            key_file=self._ssh_key_file,
            config_file=self._ssh_config_file,
            known_hosts_file=self._known_hosts_file,
            known_hosts_content=config.ssh_known_hosts,
            proxy_config=self._charm.state.proxy_config,
        )

        remote = utils.extract_remote(config.static_assets_git_repo)
        if not remote:
            return

        if not utils.remote_in_known_hosts(remote, config.ssh_known_hosts):
            logger.error("Git remote '%s' is not in the known hosts configuration.", remote)
            raise MediaWikiBlockedStatusException(
                f"Git remote '{remote}' is not in the known hosts configuration."
            )
