# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for metrics relation state."""

import pytest
from ops import CharmBase, testing

from relations.metrics import Metrics


class WrapperCharm(CharmBase):
    """A minimal charm exposing the metrics relation helper."""

    RELATION_NAME = "metrics-endpoint"

    def __init__(self, *args):
        super().__init__(*args)
        self.metrics = Metrics(
            self, self.RELATION_NAME, jobs=[], refresh_event=[self.on.config_changed]
        )


@pytest.fixture
def ctx(meta: dict) -> testing.Context:
    """Provide a context using the charm's metadata."""
    meta = meta.copy()
    config = meta.pop("config", None)
    actions = meta.pop("actions", None)
    return testing.Context(WrapperCharm, meta=meta, config=config, actions=actions)


@pytest.mark.parametrize("relation_count", [0, 1, 2])
def test_has_relation(ctx: testing.Context, relation_count: int) -> None:
    """Return whether at least one consumer is related."""
    relations = [testing.Relation(WrapperCharm.RELATION_NAME) for _ in range(relation_count)]
    with ctx(ctx.on.update_status(), testing.State(relations=relations)) as manager:
        assert manager.charm.metrics.has_relation() is (relation_count > 0)


@pytest.mark.parametrize("remaining_consumer", [False, True])
def test_broken_relation(ctx: testing.Context, remaining_consumer: bool) -> None:
    """Ignore the departing relation without losing any remaining consumer."""
    departing = testing.Relation(WrapperCharm.RELATION_NAME)
    relations = [departing]
    if remaining_consumer:
        relations.append(testing.Relation(WrapperCharm.RELATION_NAME))
    with ctx(ctx.on.relation_broken(departing), testing.State(relations=relations)) as manager:
        assert manager.charm.metrics.has_relation() is remaining_consumer


def test_custom_relation_name(meta: dict) -> None:
    """Use the configured endpoint rather than a hard-coded relation name."""

    class RenamedMetricsCharm(WrapperCharm):
        RELATION_NAME = "custom-metrics"

    meta = meta.copy()
    meta["provides"] = dict(meta["provides"])
    meta["provides"]["custom-metrics"] = meta["provides"].pop("metrics-endpoint")
    config = meta.pop("config", None)
    actions = meta.pop("actions", None)
    ctx = testing.Context(RenamedMetricsCharm, meta=meta, config=config, actions=actions)
    relation = testing.Relation("custom-metrics")
    with ctx(ctx.on.update_status(), testing.State(relations=[relation])) as manager:
        assert manager.charm.metrics.has_relation()
