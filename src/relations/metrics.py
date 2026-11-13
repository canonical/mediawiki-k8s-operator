# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Handle the Prometheus metrics relation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointProvider
from ops import CharmBase, Object

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from ops.framework import BoundEvent


class Metrics(Object):
    """Provide scrape jobs and expose the metrics relation state."""

    def __init__(
        self,
        charm: CharmBase,
        relation_name: str,
        *,
        jobs: list[dict[str, Any]],
        lookaside_jobs_callable: Callable[[], list[dict[str, Any]]] | None = None,
        refresh_event: list[BoundEvent] | None = None,
    ) -> None:
        """Initialize the metrics provider.

        Args:
            charm: The parent charm.
            relation_name: The metrics relation endpoint name.
            jobs: The scrape jobs advertised to consumers.
            lookaside_jobs_callable: An optional source of additional scrape jobs.
            refresh_event: Events that trigger a scrape-job refresh.
        """
        super().__init__(charm, "metrics-observer")
        self._relation_name = relation_name
        self._provider = MetricsEndpointProvider(
            charm,
            relation_name=relation_name,
            jobs=jobs,
            lookaside_jobs_callable=lookaside_jobs_callable,
            refresh_event=refresh_event,
        )

    def has_relation(self) -> bool:
        """Return whether any metrics consumer is related."""
        return bool(self.model.relations[self._relation_name])
