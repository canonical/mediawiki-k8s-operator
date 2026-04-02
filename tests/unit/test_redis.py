# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import pytest
from ops import CharmBase, testing
from pytest_mock import MockerFixture, MockType

import redis as redis_module


class WrapperCharm(CharmBase):
    """A minimal wrapper charm class to build a testing context with."""

    def __init__(self, *args):
        super().__init__(*args)
        self.redis = redis_module.Redis(self, "redis")


@pytest.fixture(autouse=True)
def mock_redis_requires(mocker: MockerFixture) -> MockType:
    """Fixture to mock the RedisRequires class from charms.redis_k8s.v0.redis."""
    mock_cls = mocker.patch("redis.RedisRequires")
    mock_instance = mock_cls.return_value
    mock_instance.relation_name = "redis"
    mock_instance.relation_data = None
    mock_instance.app_data = None
    return mock_instance


@pytest.fixture
def ctx(meta: dict) -> testing.Context:
    """Provide a Context with the WrapperCharm."""
    meta = meta.copy()
    config = meta.pop("config", None)
    actions = meta.pop("actions", None)
    return testing.Context(WrapperCharm, meta=meta, config=config, actions=actions)


@pytest.fixture
def redis_relation() -> testing.Relation:
    """Provide a Redis relation."""
    return testing.Relation(
        "redis",
        id=0,
        remote_app_name="redis-k8s",
    )


@pytest.fixture
def state_with_redis(redis_relation: testing.Relation) -> testing.State:
    """Provide a state with a Redis relation."""
    return testing.State(
        relations=[redis_relation],
        leader=True,
    )


@pytest.fixture
def base_state() -> testing.State:
    """Provide a state without any relations."""
    return testing.State(leader=True)


class TestIsRelationAvailable:
    """Tests for the Redis.is_relation_available method."""

    def test_returns_true_when_relation_exists(
        self,
        ctx: testing.Context,
        state_with_redis: testing.State,
    ):
        with ctx(ctx.on.update_status(), state_with_redis) as mgr:
            assert mgr.charm.redis.is_relation_available() is True

    def test_returns_false_when_no_relation(
        self,
        ctx: testing.Context,
        base_state: testing.State,
    ):
        with ctx(ctx.on.update_status(), base_state) as mgr:
            assert mgr.charm.redis.is_relation_available() is False


class TestGetEndpoint:
    """Tests for the Redis.get_endpoint method."""

    def test_returns_none_when_no_relation(
        self,
        ctx: testing.Context,
        base_state: testing.State,
    ):
        with ctx(ctx.on.update_status(), base_state) as mgr:
            assert mgr.charm.redis.get_endpoint() is None

    def test_returns_none_when_relation_data_missing(
        self,
        ctx: testing.Context,
        state_with_redis: testing.State,
    ):
        with ctx(ctx.on.update_status(), state_with_redis) as mgr:
            assert mgr.charm.redis.get_endpoint() is None

    def test_returns_none_when_hostname_missing(
        self,
        ctx: testing.Context,
        state_with_redis: testing.State,
        mock_redis_requires: MockType,
    ):
        mock_redis_requires.relation_data = {"port": "6379"}
        mock_redis_requires.app_data = None

        with ctx(ctx.on.update_status(), state_with_redis) as mgr:
            assert mgr.charm.redis.get_endpoint() is None

    def test_returns_none_when_port_missing(
        self,
        ctx: testing.Context,
        state_with_redis: testing.State,
        mock_redis_requires: MockType,
    ):
        mock_redis_requires.relation_data = {"hostname": "redis-host"}
        mock_redis_requires.app_data = None

        with ctx(ctx.on.update_status(), state_with_redis) as mgr:
            assert mgr.charm.redis.get_endpoint() is None

    def test_returns_endpoint_from_unit_data(
        self,
        ctx: testing.Context,
        state_with_redis: testing.State,
        mock_redis_requires: MockType,
    ):
        mock_redis_requires.relation_data = {"hostname": "redis-host", "port": "6379"}
        mock_redis_requires.app_data = None

        with ctx(ctx.on.update_status(), state_with_redis) as mgr:
            assert mgr.charm.redis.get_endpoint() == "redis-host:6379"

    def test_leader_host_overrides_unit_hostname(
        self,
        ctx: testing.Context,
        state_with_redis: testing.State,
        mock_redis_requires: MockType,
    ):
        mock_redis_requires.relation_data = {"hostname": "redis-unit", "port": "6379"}
        mock_redis_requires.app_data = {"leader-host": "redis-leader"}

        with ctx(ctx.on.update_status(), state_with_redis) as mgr:
            assert mgr.charm.redis.get_endpoint() == "redis-leader:6379"

    def test_falls_back_to_unit_hostname_when_no_leader_host(
        self,
        ctx: testing.Context,
        state_with_redis: testing.State,
        mock_redis_requires: MockType,
    ):
        mock_redis_requires.relation_data = {"hostname": "redis-unit", "port": "6379"}
        mock_redis_requires.app_data = {"other-key": "value"}

        with ctx(ctx.on.update_status(), state_with_redis) as mgr:
            assert mgr.charm.redis.get_endpoint() == "redis-unit:6379"
