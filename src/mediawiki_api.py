# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""MediaWiki API client for querying site information."""

import logging
import string
from typing import Any
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 15


def _api_query(**params: str) -> dict[str, Any]:
    """Make a MediaWiki API query and return the parsed JSON response.

    ``format`` is always set to ``json``.  ``formatversion`` defaults to
    ``"2"`` but can be overridden via *params*.

    Returns:
        The parsed JSON response as a dict, or an empty dict on any
        failure (network error, non-200 status, or JSON decode error).
    """
    params.setdefault("formatversion", "2")
    params["format"] = "json"
    try:
        response = requests.get(
            "http://localhost/w/api.php",
            params=params,
            timeout=_REQUEST_TIMEOUT,
        )
    except requests.exceptions.RequestException as e:
        logger.error("MediaWiki API request failed: %s", e)
        return {}

    if response.status_code != 200:
        logger.error(
            "MediaWiki API responded with status code %s",
            response.status_code,
        )
        return {}

    try:
        return response.json()
    except requests.exceptions.JSONDecodeError:
        logger.error("Failed to decode MediaWiki API response as JSON")
        return {}


class SiteInfo:
    """Parsed result of a MediaWiki ``action=query&meta=siteinfo`` request.

    Use :meth:`fetch` to query the API::

        info = SiteInfo.fetch()
        print(info.version)
        print(info.article_url)
    """

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    @classmethod
    def fetch(cls) -> "SiteInfo":
        """Query the MediaWiki siteinfo API and return a :class:`SiteInfo`."""
        result = _api_query(action="query", meta="siteinfo", siprop="general|namespaces")
        return cls(result.get("query", {}))

    @property
    def version(self) -> str:
        """The running MediaWiki version as a lowercase, hyphen-separated string.

        The string is modified to be Juju status-friendly (lowercase, no spaces).

        Returns an empty string if the version cannot be determined.
        """
        version = self._data.get("general", {}).get("generator", "")
        return "-".join(version.lower().split())

    @property
    def article_path(self) -> str | None:
        """The article path as provided by the MediaWiki API, or ``None`` if unavailable."""
        return self._data.get("general", {}).get("articlepath")

    @property
    def server(self) -> str | None:
        """The server URL as provided by the MediaWiki API, or ``None`` if unavailable."""
        return self._data.get("general", {}).get("server")

    @property
    def article_url(self) -> string.Template | None:
        """A URL template for article paths.

        The MediaWiki ``$1`` placeholder is translated to ``${article}``
        so callers can substitute using :class:`string.Template`, e.g.
        ``info.article_url.substitute(article="Main_Page")``.

        If ``articlepath`` is a relative path, it is combined with
        ``server`` to form a full URL.  If ``articlepath`` is already
        absolute or protocol-relative, it is used as-is.

        Returns ``None`` when ``articlepath`` is absent, or when it is
        relative and ``server`` is also absent.
        """
        general = self._data.get("general", {})
        path = general.get("articlepath")
        if path is None:
            return None
        parsed = urlparse(path)
        if parsed.scheme or parsed.netloc:
            full_url = path
        else:
            server = general.get("server")
            if server is None:
                return None
            full_url = server + path
        return string.Template(full_url.replace("$1", "${article}"))

    @property
    def special_namespace_name(self) -> str | None:
        """The localized name of the ``Special`` namespace (e.g. ``"Spécial"``).

        Returns ``None`` if namespace information is unavailable.
        """
        namespaces = self._data.get("namespaces", {})
        ns = namespaces.get("-1", {})
        return ns.get("name") or None
