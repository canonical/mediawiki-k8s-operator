# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Discover the OTLP/HTTP receiver used by MediaWiki's native tracer."""

from urllib.parse import urlsplit, urlunsplit

from charmlibs.interfaces.tracing import TracingEndpointRequirer, TracingEndpointRequirerEvents
from ops import Object

from exceptions import MediaWikiWaitingStatusException
from state import StatefulCharmBase


class Tracing(Object):
    """Manage optional workload tracing endpoint discovery."""

    def __init__(self, charm: StatefulCharmBase, relation_name: str):
        """Initialize the tracing requirer.

        Args:
            charm: The parent charm.
            relation_name: The tracing relation endpoint name.
        """
        super().__init__(charm, "tracing-observer")
        self._relation_name = relation_name
        self.tracing = TracingEndpointRequirer(
            charm, relation_name=relation_name, protocols=["otlp_http"]
        )

    @property
    def on(self) -> TracingEndpointRequirerEvents:
        """Expose the library's endpoint lifecycle events."""
        return self.tracing.on

    @property
    def relation_name(self) -> str:
        """Return the tracing relation name."""
        return self._relation_name

    def has_relation(self) -> bool:
        """Return whether the tracing relation is established."""
        return self.model.get_relation(self.relation_name) is not None

    def get_endpoint(self) -> str | None:
        """Return the traces URL, waiting for related providers to supply a usable receiver.

        Returns:
            The OTLP/HTTP traces URL, or None when there is no active tracing relation.

        Raises:
            MediaWikiWaitingStatusException: If an active relation has no usable receiver.
        """
        if not self.has_relation():
            return None
        data = self.tracing.get_all_endpoints()
        if data is None:
            raise MediaWikiWaitingStatusException("Waiting for tracing relation data")
        # get_endpoint reads the local app databag when a receiver is missing,
        # which followers cannot access. Select from the validated public data instead.
        endpoint = next(
            (receiver.url for receiver in data.receivers if receiver.protocol.name == "otlp_http"),
            None,
        )
        if not endpoint:
            raise MediaWikiWaitingStatusException(
                "Waiting for an OTLP/HTTP receiver on the tracing relation"
            )
        try:
            parsed = urlsplit(endpoint)
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or any(character.isspace() or ord(character) < 32 for character in endpoint)
            ):
                raise ValueError("expected an HTTP(S) URL without credentials, query, or fragment")
            _ = parsed.port
        except ValueError as error:
            raise MediaWikiWaitingStatusException(
                "Waiting for a valid OTLP/HTTP endpoint on the tracing relation"
            ) from error
        path = parsed.path.rstrip("/")
        if not path.endswith("/v1/traces"):
            path += "/v1/traces"
        return urlunsplit(parsed._replace(path=path))
