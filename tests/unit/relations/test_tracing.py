# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for workload tracing discovery and relation lifecycle."""

import dataclasses
import json

import pytest
from ops import EventBase, testing

from exceptions import MediaWikiWaitingStatusException
from relations.tracing import Tracing
from state import StatefulCharmBase


class TracingCharm(StatefulCharmBase):
    """Isolate the tracing relation from other workload prerequisites."""

    RELATION_NAME = "tracing"

    def __init__(self, *args):
        super().__init__(*args)
        self.tracing = Tracing(self, self.RELATION_NAME)
        self.endpoint = None
        for event in (
            self.on.update_status,
            self.tracing.on.endpoint_changed,
            self.tracing.on.endpoint_removed,
        ):
            self.framework.observe(event, self._reconcile)

    def _reconcile(self, _: EventBase) -> None:
        self.endpoint = self.tracing.get_endpoint()


@pytest.fixture
def ctx(meta: dict) -> testing.Context:
    """Build a lightweight tracing context using actual charm metadata."""
    meta = meta.copy()
    config = meta.pop("config")
    actions = meta.pop("actions")
    return testing.Context(TracingCharm, meta=meta, config=config, actions=actions)


def receiver_relation(url: str, protocol: str = "otlp_http") -> testing.Relation:
    """Build provider data using the tracing interface shape."""
    return testing.Relation(
        "tracing",
        remote_app_data={
            "receivers": json.dumps([{"protocol": {"name": protocol, "type": "http"}, "url": url}])
        },
    )


@pytest.mark.parametrize("leader", [False, True])
def test_requests_only_otlp_http(
    ctx: testing.Context, tracing_relation: testing.Relation, leader: bool
) -> None:
    """All units consume endpoints, but only the leader publishes protocol requests."""
    state = testing.State(relations=[tracing_relation], leader=leader)
    out = ctx.run(ctx.on.relation_changed(tracing_relation), state)
    app_data = out.get_relation(tracing_relation.id).local_app_data
    assert app_data == ({"receivers": '["otlp_http"]'} if leader else {})


@pytest.mark.parametrize(
    "url, expected",
    [
        ("http://collector:4318", "http://collector:4318/v1/traces"),
        ("https://collector:4318/", "https://collector:4318/v1/traces"),
        ("https://collector/prefix", "https://collector/prefix/v1/traces"),
        ("http://collector/v1/traces", "http://collector/v1/traces"),
        ("http://collector/prefix/v1/traces/", "http://collector/prefix/v1/traces"),
        ("http://[::1]:4318", "http://[::1]:4318/v1/traces"),
    ],
)
def test_endpoint(ctx: testing.Context, url: str, expected: str) -> None:
    """Preserve transport and ingress prefixes without duplicating the ingestion path."""
    relation = receiver_relation(url)
    with ctx(ctx.on.relation_changed(relation), testing.State(relations=[relation])) as mgr:
        mgr.run()
        assert mgr.charm.endpoint == expected
        assert mgr.charm.tracing.has_relation()


@pytest.mark.parametrize(
    "url",
    [
        "grpc://collector:4317",
        "collector:4318",
        "http://",
        "http://user:password@collector",
        "http://collector:invalid",
        "http://collector:99999",
        "http://collector?token=value",
        "http://collector/#fragment",
        "http://col lector",
        "http://collector/\n",
        "http://[invalid",
    ],
)
def test_invalid_endpoint(ctx: testing.Context, url: str) -> None:
    """Raise Waiting immediately when the provider publishes a malformed receiver URL."""
    relation = receiver_relation(url)
    with pytest.raises(testing.errors.UncaughtCharmError, match="valid OTLP/HTTP") as error:
        ctx.run(ctx.on.relation_changed(relation), testing.State(relations=[relation]))
    assert isinstance(error.value.__cause__, MediaWikiWaitingStatusException)


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"receivers": "not-json"},
        {"receivers": '[{"invalid": true}]'},
        {"receivers": "[]"},
        {
            "receivers": '[{"protocol": {"name": "zipkin", "type": "http"}, '
            '"url": "http://collector:9411"}]'
        },
    ],
)
def test_unavailable_provider(ctx: testing.Context, data: dict[str, str]) -> None:
    """Missing, malformed or incompatible provider data immediately raises Waiting."""
    relation = testing.Relation(
        "tracing", remote_app_data=data, local_app_data={"receivers": '["otlp_http"]'}
    )
    with pytest.raises(testing.errors.UncaughtCharmError) as error:
        ctx.run(ctx.on.relation_changed(relation), testing.State(relations=[relation]))
    assert isinstance(error.value.__cause__, MediaWikiWaitingStatusException)


def test_no_relation(ctx: testing.Context) -> None:
    """No tracing integration has no tracing readiness requirement."""
    with ctx(ctx.on.update_status(), testing.State()) as mgr:
        mgr.run()
        assert mgr.charm.endpoint is None
        assert not mgr.charm.tracing.has_relation()


def test_broken_relation_ignores_stale_data(
    ctx: testing.Context, tracing_relation: testing.Relation
) -> None:
    """Removal clears tracing even if provider data was complete before the broken event."""
    state = testing.State(relations=[tracing_relation])
    with ctx(ctx.on.relation_broken(tracing_relation), state) as mgr:
        mgr.run()
        assert mgr.charm.endpoint is None
        assert not mgr.charm.tracing.has_relation()


def test_custom_relation_name(meta: dict, tracing_relation: testing.Relation) -> None:
    """Use the configured endpoint name for both relation lookup and the requirer."""

    class RenamedTracingCharm(TracingCharm):
        RELATION_NAME = "workload-tracing"

    meta = meta.copy()
    meta["requires"] = dict(meta["requires"])
    meta["requires"]["workload-tracing"] = meta["requires"].pop("tracing")
    config = meta.pop("config")
    actions = meta.pop("actions")
    ctx = testing.Context(RenamedTracingCharm, meta=meta, config=config, actions=actions)
    relation = dataclasses.replace(tracing_relation, endpoint="workload-tracing")
    with ctx(ctx.on.relation_changed(relation), testing.State(relations=[relation])) as manager:
        manager.run()
        assert manager.charm.tracing.relation_name == "workload-tracing"
        assert manager.charm.tracing.has_relation()
        assert manager.charm.endpoint == "http://collector:4318/v1/traces"


def test_endpoint_changes(ctx: testing.Context, tracing_relation: testing.Relation) -> None:
    """Read the latest provider data rather than persisting a stale endpoint."""
    state = ctx.run(
        ctx.on.relation_changed(tracing_relation), testing.State(relations=[tracing_relation])
    )
    updated = dataclasses.replace(
        state.get_relation(tracing_relation.id),
        remote_app_data=receiver_relation("https://new-collector/prefix").remote_app_data,
    )
    with ctx(
        ctx.on.relation_changed(updated), dataclasses.replace(state, relations=[updated])
    ) as mgr:
        mgr.run()
        assert mgr.charm.endpoint == "https://new-collector/prefix/v1/traces"
