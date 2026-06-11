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
        lock_content: Optional[str] = None,
        force: bool = False,
    ) -> None:
        """Reconcile the composer configuration.

        Routing is determined by the unit's leadership role:

        **Leader path**: Writes ``composer.user.json`` and runs ``composer update``.
        The ``lock_content`` parameter is ignored.

        **Non-leader path**: If ``lock_content`` is None the unit cannot proceed and raises
        :exc:`MediaWikiWaitingStatusException`. Otherwise writes both ``composer.user.json``
        and ``composer.lock`` before running ``composer install``.

        In both paths the operation is skipped when the relevant files already match their
        expected content (unless ``force`` is True).

        Args:
            composer_json: Desired content for ``composer.user.json`` as a dict.
            lock_content: Pre-generated lock file content from the peer leader. Ignored on
                the leader; required (non-None) on non-leaders.
            force: If True, skip the content-diff check and always run composer.

        Raises:
            MediaWikiWaitingStatusException: If this is a non-leader unit and no lock has
                been published by the leader yet.
            MediaWikiBlockedStatusException: If the composer command fails.
        """
        is_leader = self._charm.unit.is_leader()

        # Non-leaders require the lock published by the leader. Capture it in a separately
        # typed variable so the later write is statically known to be non-None.
        lock_to_write: Optional[str] = None
        if not is_leader:
            if lock_content is None:
                raise MediaWikiWaitingStatusException(
                    "Waiting for leader to publish composer lock"
                )
            lock_to_write = lock_content

        # Determine whether we can skip this reconciliation.
        if not force and self._should_skip_composer(
            composer_json, is_leader=is_leader, lock_content=lock_content
        ):
            logger.debug(
                "Composer configuration%s unchanged, skipping %s.",
                "" if is_leader else " and lock",
                "update" if is_leader else "install",
            )
            return

        subcommand = "update" if is_leader else "install"
        logger.info(
            "Starting composer reconciliation (%s: %s).",
            "leader" if is_leader else "non-leader",
            subcommand,
        )

        self._user_composer_file.write_text(
            json.dumps(composer_json),
            mode=0o640,
            user=constants.WEBROOT_OWNER_USER,
            group=constants.DAEMON_GROUP,
        )

        # Non-leaders also write the lock file before install.
        if lock_to_write is not None:
            self._composer_lock_file.write_text(
                lock_to_write,
                mode=0o640,
                user=constants.WEBROOT_OWNER_USER,
                group=constants.DAEMON_GROUP,
            )

        result = self._run_cli(
            [str(self._composer_path), subcommand, "--no-dev", "--optimize-autoloader"],
            user=constants.WEBROOT_OWNER_USER,
            group=constants.DAEMON_GROUP,
            working_dir=str(self._mediawiki_path),
            environment=self._charm.state.get_proxy_env(),
            timeout=constants.LONG_TIMEOUT * 2,
        )

        if result.return_code != 0:
            self._handle_composer_failure(composer_json, is_leader=is_leader)
            result.raise_for_status(f"Composer {subcommand}", MediaWikiBlockedStatusException)

        logger.info("Composer %s completed successfully:\n%s", subcommand, result.stdout)

    def _handle_composer_failure(self, composer_json: dict[str, Any], *, is_leader: bool) -> None:
        """Write a marker after a failed composer command so that the next reconciliation retries.

        For leaders, a ``_charm_error`` key is added to ``composer.user.json``.
        For non-leaders, the lock file is cleared.
        """
        if is_leader:
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
        is_leader: bool,
        lock_content: Optional[str],
    ) -> bool:
        """Return whether or not composer reconciliation can be skipped.

        For leaders, this is true if the current ``composer.user.json`` matches the desired state. For non-leaders, this is true if both the current ``composer.user.json`` matches
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

        if is_leader:
            return True

        if not self._composer_lock_file.exists():
            return False
        return self._composer_lock_file.read_text() == lock_content
