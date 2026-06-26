# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared base declaring the interface used by the MediaWiki workload mixins."""

from __future__ import annotations

from typing import TYPE_CHECKING, List

from charmlibs.pathops import ContainerPath

from container import ContainerService
from mediawiki import constants

if TYPE_CHECKING:
    from auth import OAuth, Saml
    from database import Database
    from redis import Redis
    from s3 import S3
    from smtp import Smtp
    from state import StatefulCharmBase
    from types_ import CommandExecResult


class _MediaWikiBase(ContainerService):
    """Base class declaring shared state and behaviour for the MediaWiki workload mixins.

    The collaborator objects declared here are assigned by
    :class:`mediawiki._core.MediaWiki`. Container paths used by more than one mixin
    (or by both the core class and a mixin) are exposed as properties built from the
    shared :mod:`mediawiki.constants`. Paths used by a single mixin are declared on
    that mixin instead.
    """

    # Collaborator objects (assigned by MediaWiki.__init__)
    _charm: StatefulCharmBase
    _database: Database
    _oauth: OAuth
    _saml: Saml
    _redis: Redis
    _s3: S3
    _smtp: Smtp

    @property
    def _composer_lock_file(self) -> ContainerPath:
        """The composer.lock file (shared by the composer mixin and core)."""
        return ContainerPath(constants.COMPOSER_LOCK_FILE, container=self._container)

    @property
    def _local_settings_file(self) -> ContainerPath:
        """The LocalSettings.php file (shared by the settings mixin and core)."""
        return ContainerPath(constants.LOCAL_SETTINGS_FILE, container=self._container)

    @property
    def _user_settings_file(self) -> ContainerPath:
        """The UserSettings.php file (shared by the settings mixin and core)."""
        return ContainerPath(constants.USER_SETTINGS_FILE, container=self._container)

    @property
    def _job_runner_config(self) -> ContainerPath:
        """The JobRunnerConfig.json file (shared by the settings mixin and core)."""
        return ContainerPath(constants.JOB_RUNNER_CONFIG_PATH, container=self._container)

    @property
    def _php_cli_path(self) -> ContainerPath:
        """The PHP CLI binary (shared by the settings mixin and core)."""
        return ContainerPath(constants.PHP_CLI_PATH, container=self._container)

    @property
    def _maintenance_scripts_base_path(self) -> ContainerPath:
        """The MediaWiki maintenance scripts directory (shared by the settings mixin and core)."""
        return ContainerPath(constants.MAINTENANCE_SCRIPTS_PATH, container=self._container)

    def _run_maintenance_script(
        self,
        args: List[str],
        timeout: int = constants.LONG_TIMEOUT,
        combine_stderr: bool = False,
        sensitive: bool = False,
    ) -> CommandExecResult:
        """Execute a MediaWiki maintenance script with the given arguments.

        This is a helper method for running maintenance scripts in the form of "php maintenance/run.php <args>".

        If timeout is exceeded, a ContainerError will be raised.
        """
        result = self._run_cli(
            [str(self._php_cli_path), str(self._maintenance_scripts_base_path / "run.php"), *args],
            environment=self._charm.state.get_proxy_env(),
            user=constants.DAEMON_USER,
            group=constants.DAEMON_GROUP,
            timeout=timeout,
            combine_stderr=combine_stderr,
            sensitive=sensitive,
        )
        return result
