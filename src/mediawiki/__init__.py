# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Package for managing and interacting with the primary MediaWiki workload/container."""

from mediawiki._core import INSTALLED_FLAG_TABLE, MediaWiki
from mediawiki._secrets import MediaWikiSecrets

__all__ = [
    "INSTALLED_FLAG_TABLE",
    "MediaWiki",
    "MediaWikiSecrets",
]
