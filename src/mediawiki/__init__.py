# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Package for managing and interacting with the primary MediaWiki workload/container."""

from mediawiki._core import MediaWiki
from mediawiki._secrets import MediaWikiSecrets

__all__ = [
    "MediaWiki",
    "MediaWikiSecrets",
]
