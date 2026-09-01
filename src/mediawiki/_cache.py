# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Localisation cache management for the MediaWiki workload."""

import logging

from charmlibs.pathops import ContainerPath

from exceptions import MediaWikiBlockedStatusException
from mediawiki import constants
from mediawiki._base import _MediaWikiBase

logger = logging.getLogger(__name__)


class _CacheMixin(_MediaWikiBase):
    """Mixin providing cache directory and localisation cache management for :class:`MediaWiki`.

    MediaWiki is configured with ``$wgLocalisationCacheConf['manualRecache'] = true``, so the
    localisation cache is only ever rebuilt when the charm runs the rebuild maintenance
    script. The rebuild is triggered when the MediaWiki version changed since the last
    successful rebuild (tracked per unit in the peer relation databag), when the settings
    files or composer.lock changed, or when a forced reconciliation is requested.
    """

    @property
    def _cache_dir(self) -> ContainerPath:
        """The MediaWiki cache directory inside the cache storage mount."""
        return ContainerPath(constants.CACHE_DIR, container=self._container)

    def _localisation_cache_reconciliation(
        self, settings_changed: bool = False, composer_ran: bool = False, *, force: bool = False
    ) -> None:
        """Rebuild the MediaWiki localisation cache when any of its inputs changed.

        Args:
            settings_changed: Whether the settings files' content changed this cycle.
            composer_ran: Whether composer re-resolved dependencies this cycle.
            force: Whether to do a force rebuild all localisation cache entries.

        Raises:
            MediaWikiBlockedStatusException: If the rebuild fails. The peer databag is left
                untouched so the rebuild is retried on the next reconciliation.
        """
        current_version = self.version()
        stored_version = self._peers.localisation_cache_version()
        if (
            not force
            and stored_version == current_version
            and not settings_changed
            and not composer_ran
        ):
            logger.debug("Localisation cache is up to date, skipping rebuild.")
            return

        logger.info(
            "Rebuilding localisation cache (stored version: %s, current version: %s, "
            "settings changed: %s, composer ran: %s, forced: %s)",
            stored_version,
            current_version,
            settings_changed,
            composer_ran,
            force,
        )
        result = self._run_maintenance_script(
            ["rebuildLocalisationCache", "--force"] if force else ["rebuildLocalisationCache"]
        )
        result.raise_for_status("Localisation cache rebuild", MediaWikiBlockedStatusException)
        logger.debug("Localisation cache rebuild output:\n%s", result.stdout)

        self._peers.mark_localisation_cache_rebuilt(current_version)
