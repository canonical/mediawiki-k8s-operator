# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Composer reconciliation logic for the MediaWiki workload."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from charmlibs.pathops import ContainerPath

from exceptions import (
    MediaWikiBlockedStatusException,
    MediaWikiWaitingStatusException,
)
from mediawiki import constants
from mediawiki._base import _MediaWikiBase

logger = logging.getLogger(__name__)


class _ComposerMixin(_MediaWikiBase):
    """Mixin providing composer.json/composer.lock reconciliation for :class:`MediaWiki`."""

    @property
    def _mediawiki_path(self) -> ContainerPath:
        """The MediaWiki application directory used as the composer working directory."""
        return ContainerPath(constants.MEDIAWIKI_PATH, container=self._container)

    @property
    def _user_composer_file(self) -> ContainerPath:
        """The composer.user.json file managed by the charm."""
        return ContainerPath(constants.USER_COMPOSER_FILE, container=self._container)

    @property
    def _composer_path(self) -> ContainerPath:
        """The composer binary."""
        return ContainerPath(constants.COMPOSER_PATH, container=self._container)

    def _composer_reconciliation(
        self,
        composer_json: dict[str, Any],
        *,
        update_dependencies: bool,
        lock_content: Optional[str] = None,
        force: bool = False,
    ) -> bool:
        """Reconcile the composer configuration.

        The caller selects whether to resolve dependencies locally or install from a lock.
        Leadership and workload recovery eligibility are not checked here.

        **Update path**: Writes ``composer.user.json`` and runs ``composer update``.
        The ``lock_content`` parameter is ignored.

        **Install path**: If ``lock_content`` is None the unit cannot proceed and raises
        :exc:`MediaWikiWaitingStatusException`. Otherwise writes both ``composer.user.json``
        and ``composer.lock`` before running ``composer install``.

        In both paths the operation is skipped when the relevant files already match their
        expected content (unless ``force`` is True).

        Args:
            composer_json: Desired content for ``composer.user.json`` as a dict.
            update_dependencies: Whether to resolve dependencies with update rather than
                install from the supplied lock.
            lock_content: Pre-generated lock file content from the peer leader. Ignored
                during local resolution; otherwise required (non-None).
            force: If True, skip the content-diff check and always run composer.

        Returns:
            Whether composer re-resolved dependencies (``update``/``install`` ran). False
            when the operation was skipped because the configuration is unchanged. Note
            that a run may still produce an identical lock; the rebuild consuming this
            signal is cheap and incremental, so that trade-off is accepted for simplicity.

        Raises:
            MediaWikiWaitingStatusException: If install was selected without a lock.
            MediaWikiBlockedStatusException: If the composer command fails.
        """
        # Installs require a lock. Capture it in a separately
        # typed variable so the later write is statically known to be non-None.
        lock_to_write: Optional[str] = None
        if not update_dependencies:
            if lock_content is None:
                raise MediaWikiWaitingStatusException(
                    "Waiting for leader to publish composer lock"
                )
            lock_to_write = lock_content

        # Determine whether we can skip this reconciliation.
        if not force and self._should_skip_composer(
            composer_json, update_dependencies=update_dependencies, lock_content=lock_content
        ):
            logger.debug(
                "Composer configuration%s unchanged, skipping %s.",
                "" if update_dependencies else " and lock",
                "update" if update_dependencies else "install",
            )
            return False

        subcommand = "update" if update_dependencies else "install"
        logger.info(
            "Starting composer reconciliation (%s: %s).",
            "local resolution" if update_dependencies else "leader lock",
            subcommand,
        )

        self._user_composer_file.write_text(
            json.dumps(composer_json),
            mode=0o640,
            user=constants.WEBROOT_OWNER_USER,
            group=constants.DAEMON_GROUP,
        )

        # Write the supplied lock file before install.
        if lock_to_write is not None:
            self._composer_lock_file.write_text(
                lock_to_write,
                mode=0o640,
                user=constants.WEBROOT_OWNER_USER,
                group=constants.DAEMON_GROUP,
            )

        environment = self._charm.state.get_proxy_env() or {}
        environment["COMPOSER_DISCARD_CHANGES"] = "true"

        result = self._run_cli(
            [str(self._composer_path), subcommand, "--no-dev", "--optimize-autoloader"],
            user=constants.WEBROOT_OWNER_USER,
            group=constants.DAEMON_GROUP,
            working_dir=str(self._mediawiki_path),
            environment=environment,
            timeout=constants.LONG_TIMEOUT * 2,
        )

        if result.return_code != 0:
            self._handle_composer_failure(composer_json, update_dependencies=update_dependencies)
            result.raise_for_status(f"Composer {subcommand}", MediaWikiBlockedStatusException)

        logger.info("Composer %s completed successfully:\n%s", subcommand, result.stdout)
        return True

    def _handle_composer_failure(
        self, composer_json: dict[str, Any], *, update_dependencies: bool
    ) -> None:
        """Write a marker after a failed composer command so that the next reconciliation retries.

        For local resolution, a ``_charm_error`` key is added to ``composer.user.json``.
        For installs from the leader lock, the lock file is cleared.
        """
        if update_dependencies:
            failed = {**composer_json, "_charm_error": "Composer update failed"}
            self._user_composer_file.write_text(
                json.dumps(failed),
                mode=0o640,
                user=constants.WEBROOT_OWNER_USER,
                group=constants.DAEMON_GROUP,
            )
        else:
            self._composer_lock_file.write_text(
                "",
                mode=0o640,
                user=constants.WEBROOT_OWNER_USER,
                group=constants.DAEMON_GROUP,
            )

    def _should_skip_composer(
        self,
        composer_json: dict[str, Any],
        *,
        update_dependencies: bool,
        lock_content: Optional[str],
    ) -> bool:
        """Return whether or not composer reconciliation can be skipped.

        For local resolution, this is true if the current ``composer.user.json`` matches the desired state. For installs from the leader lock, this is true if both the current ``composer.user.json`` matches
        the desired state and the current ``composer.lock`` matches the leader-published lock.
        """
        if not self._user_composer_file.exists():
            return not composer_json
        try:
            current_json = json.loads(self._user_composer_file.read_text())
        except json.JSONDecodeError:
            return False

        if current_json != composer_json:
            return False

        if update_dependencies:
            return True

        if not self._composer_lock_file.exists():
            return False
        return self._composer_lock_file.read_text() == lock_content
