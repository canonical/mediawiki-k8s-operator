# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared base declaring the interface used by the MediaWiki workload mixins."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, List

from charmlibs.pathops import ContainerPath

from container import ContainerService
from exceptions import MediaWikiInstallError
from mediawiki import constants

if TYPE_CHECKING:
    from auth import OAuth, Saml
    from cache import Cache
    from database import Database
    from egress import TunnelServiceRegistry
    from mediawiki_peers import MediaWikiPeers
    from s3 import S3
    from smtp import Smtp
    from state import StatefulCharmBase
    from types_ import CommandExecResult

logger = logging.getLogger(__name__)


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
    _cache: Cache
    _database: Database
    _oauth: OAuth
    _saml: Saml
    _s3: S3
    _smtp: Smtp
    _peers: MediaWikiPeers
    _tunnel_services: TunnelServiceRegistry
    _SMTP_PROXY_SERVICE_NAME: str
    _SMTP_PROXY_PORT: int

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

    def version(self) -> str:
        """Get the MediaWiki version running in the workload container.

        Reads the ``MW_VERSION`` constant from ``includes/Defines.php`` rather than running
        any MediaWiki code: the ``Version`` maintenance script bootstraps MediaWiki (loading
        LocalSettings.php, extensions, and the localisation cache) and its output is
        localised, whereas the constant is a static, language-independent identifier that is
        always present in the image, even before the localisation cache is built.

        Returns:
            The MediaWiki version string (e.g. ``"1.46.0"``).

        Raises:
            MediaWikiInstallError: If the version cannot be determined, which indicates a
                broken workload image rather than a transient condition.
        """
        defines_file = ContainerPath(constants.DEFINES_FILE, container=self._container)
        content = defines_file.read_text() if defines_file.exists() else ""
        match = re.search(r"define\(\s*'MW_VERSION',\s*'([^']+)'", content)
        if not match:
            logger.error("Unable to find MW_VERSION in %s", constants.DEFINES_FILE)
            raise MediaWikiInstallError(
                "Unable to determine the MediaWiki version from the workload"
            )
        return match.group(1)
