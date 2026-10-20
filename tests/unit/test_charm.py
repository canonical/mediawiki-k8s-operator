# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import copy
import dataclasses
import json
from typing import Any

import ops
import pytest
from ops import pebble, testing
from pytest_mock import MockerFixture, MockType

from charm import Charm
from exceptions import (
    MediaWikiBlockedStatusException,
    MediaWikiInstallError,
    MediaWikiWaitingStatusException,
)
from mediawiki import MediaWiki as WorkloadMediaWiki
from mediawiki import MediaWikiSecrets
from mediawiki_api import SiteInfo
from mediawiki_peers import MediaWikiPeers
from tests.unit.conftest import MOCK_COMPOSER_LOCK
from types_ import CommandExecResult, OperationMode


@pytest.fixture(autouse=True)
def mock_mediawiki(mocker: MockerFixture) -> MockType:
    """Base MediaWiki class mock."""
    # We patch where it is imported in the charm.py file, not where it is defined
    mock_mediawiki_cls = mocker.patch("charm.MediaWiki", autospec=True)
    mock_instance = mock_mediawiki_cls.return_value

    def reconcile_with_workload(*args, **kwargs):
        """Run the real workload loop while retaining a mock at the Charm boundary."""
        constructor_args = mock_mediawiki_cls.call_args.args
        constructor_kwargs = mock_mediawiki_cls.call_args.kwargs
        workload = WorkloadMediaWiki(*constructor_args, **constructor_kwargs)
        workload._reconcile_configuration = mock_instance._reconcile_configuration
        workload._settings_reconciliation = mock_instance._settings_reconciliation
        workload.update_database_schema = mock_instance.update_database_schema
        workload.runner_queue_service_is_ready = mock_instance.runner_queue_service_is_ready
        return workload.reconciliation(*args, **kwargs)

    # Setup default return values or side effects
    mock_instance.reconciliation.side_effect = reconcile_with_workload
    mock_instance.create_and_promote_user.return_value = "mocked-password"
    mock_instance.update_database_schema.return_value = None
    mock_instance._reconcile_configuration.return_value = False
    mock_instance.runner_queue_service_is_ready.return_value = False
    mock_instance.version.return_value = "1.46.0"
    mock_instance.run_maintenance_script.return_value = CommandExecResult(
        return_code=0, stdout="mocked-output\n", stderr=""
    )

    return mock_instance


@pytest.fixture(autouse=True)
def mock_git_sync(mocker: MockerFixture) -> MockType:
    """Base GitSync class mock."""
    mock_git_sync_cls = mocker.patch("charm.GitSync", autospec=True)
    mock_instance = mock_git_sync_cls.return_value

    mock_instance.is_ready.return_value = True
    mock_instance.metrics_scrape_jobs.return_value = []

    return mock_instance


@pytest.fixture(autouse=True)
def mock_site_info(mocker: MockerFixture) -> SiteInfo:
    """Mock SiteInfo.fetch to return a SiteInfo with default test data."""
    info = SiteInfo(
        {
            "general": {
                "generator": "MediaWiki 1.46.0",
                "server": "http://localhost",
                "articlepath": "/wiki/$1",
            },
            "namespaces": {"-1": {"name": "Special"}},
        }
    )
    mocker.patch.object(SiteInfo, "fetch", return_value=info)
    return info


class TestRelationEventExposure:
    """Relation helpers preserve event sources and reconciliation subscriptions."""

    @pytest.mark.parametrize(
        ("helper_name", "source_name", "event_name"),
        [
            ("_database", "db", "database_created"),
            ("_database", "db", "endpoints_changed"),
            ("_oauth", "oauth", "oauth_info_changed"),
            ("_oauth", "oauth", "oauth_info_removed"),
            ("_saml", "saml", "saml_data_available"),
            ("_s3", "s3", "credentials_changed"),
            ("_s3", "s3", "credentials_gone"),
            ("_tls", "tls", "certificate_available"),
            ("_tls", "tls", "certificate_denied"),
            ("_tracing", "tracing", "endpoint_changed"),
            ("_tracing", "tracing", "endpoint_removed"),
        ],
    )
    def test_reconciliation_subscription(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mocker: MockerFixture,
        helper_name: str,
        source_name: str,
        event_name: str,
    ) -> None:
        """Forward the original bound event to the central reconciliation handler."""
        observe = mocker.spy(ops.Framework, "observe")

        with ctx(ctx.on.update_status(), active_state) as manager:
            helper = getattr(manager.charm, helper_name)
            source = getattr(helper, source_name)
            assert helper.on is source.on
            event = getattr(helper.on, event_name)
            subscriptions = [
                call.args[1]
                for call in observe.call_args_list
                if call.args[2] == manager.charm._reconciliation
            ]
            assert (
                sum(
                    subscription.emitter is event.emitter
                    and subscription.event_kind == event.event_kind
                    and subscription.event_type is event.event_type
                    for subscription in subscriptions
                )
                == 1
            )

    def test_redis_relation_changed_subscription(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mocker: MockerFixture,
    ) -> None:
        """Reconcile on raw redis relation-changed so app-data leader-host updates apply."""
        observe = mocker.spy(ops.Framework, "observe")

        with ctx(ctx.on.update_status(), active_state) as manager:
            event = manager.charm.on[manager.charm._REDIS_RELATION_NAME].relation_changed
            assert any(
                call.args[1].emitter is event.emitter
                and call.args[1].event_kind == event.event_kind
                and call.args[2] == manager.charm._reconciliation
                for call in observe.call_args_list
            )


class TestGeneralEvents:
    def test_invalid_proxy_config(
        self, ctx: testing.Context, active_state: testing.State, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that we block should invalid proxy configurations be provided"""
        monkeypatch.setenv("JUJU_CHARM_HTTP_PROXY", "invalid")
        state_out = ctx.run(ctx.on.update_status(), active_state)
        assert isinstance(state_out.unit_status, ops.BlockedStatus)


class TestCertificatesRelationEvents:
    """Tests for the optional TLS certificates relation."""

    def test_relation_changed_without_certificate_keeps_http_active(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        certificates_relation: testing.Relation,
    ) -> None:
        """A relation without an issued certificate leaves the HTTP workload active."""
        state_in = dataclasses.replace(
            active_state, relations=[*active_state.relations, certificates_relation]
        )

        state_out = ctx.run(ctx.on.relation_changed(relation=certificates_relation), state_in)

        assert isinstance(state_out.unit_status, ops.ActiveStatus)

    def test_tls_change_requests_restart_during_service_reconciliation(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        certificates_relation: testing.Relation,
        mock_mediawiki: MockType,
        mocker: MockerFixture,
    ) -> None:
        """Changed TLS configuration requests restart through service reconciliation."""
        state_in = dataclasses.replace(
            active_state, relations=[*active_state.relations, certificates_relation]
        )
        mock_mediawiki.reconciliation.return_value = False

        state_out = ctx.run(ctx.on.relation_changed(relation=certificates_relation), state_in)

        assert isinstance(state_out.unit_status, ops.ActiveStatus)
        mock_mediawiki.reconciliation.assert_called_once()


class TestTracingEvents:
    """Unready tracing relations stop reconciliation until the provider updates its data."""

    @pytest.mark.parametrize("event_name", ["relation_created", "relation_changed"])
    def test_unavailable_endpoint_stops_reconciliation(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mock_mediawiki: MockType,
        event_name: str,
    ) -> None:
        """Stop configuration reconciliation before starting workload services."""
        relation = testing.Relation("tracing")
        state = dataclasses.replace(active_state, relations=[*active_state.relations, relation])
        event = getattr(ctx.on, event_name)(relation)
        with ctx(event, state) as manager:
            mock_mediawiki._reconcile_configuration.side_effect = lambda *args, **kwargs: (
                manager.charm._tracing.get_endpoint()
            )
            out = manager.run()
        assert out.unit_status == ops.WaitingStatus("Waiting for tracing relation data")
        assert "mediawiki" not in out.get_container("mediawiki").service_statuses
        mock_mediawiki.reconciliation.assert_called_once()

    def test_provider_recovers(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        tracing_relation: testing.Relation,
        mock_mediawiki: MockType,
    ) -> None:
        """A provider advertising its receiver clears tracing Waiting."""
        pending = dataclasses.replace(tracing_relation, remote_app_data={})
        state = dataclasses.replace(active_state, relations=[*active_state.relations, pending])
        with ctx(ctx.on.relation_changed(pending), state) as manager:
            mock_mediawiki._reconcile_configuration.side_effect = lambda *args, **kwargs: (
                manager.charm._tracing.get_endpoint()
            )
            out = manager.run()
        assert isinstance(out.unit_status, ops.WaitingStatus)
        ready = dataclasses.replace(
            out.get_relation(pending.id), remote_app_data=tracing_relation.remote_app_data
        )
        with ctx(
            ctx.on.relation_changed(ready),
            dataclasses.replace(
                out, relations=[r for r in out.relations if r.id != ready.id] + [ready]
            ),
        ) as manager:
            out = manager.run()
        assert isinstance(out.unit_status, ops.ActiveStatus)
        assert (
            out.get_container("mediawiki").service_statuses["mediawiki"]
            == pebble.ServiceStatus.ACTIVE
        )

    def test_removal_is_not_waiting(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        tracing_relation: testing.Relation,
    ) -> None:
        """Removal is an intentional return to untraced operation."""
        state = dataclasses.replace(
            active_state, relations=[*active_state.relations, tracing_relation]
        )
        out = ctx.run(ctx.on.relation_broken(tracing_relation), state)
        assert isinstance(out.unit_status, ops.ActiveStatus)

    def test_database_error_takes_precedence(
        self, ctx: testing.Context, base_state: testing.State
    ) -> None:
        """An unavailable optional tracing provider does not mask mandatory errors."""
        relation = testing.Relation("tracing")
        state = dataclasses.replace(base_state, relations=[relation])
        out = ctx.run(ctx.on.relation_changed(relation), state)
        assert isinstance(out.unit_status, ops.BlockedStatus)
        assert "database" in out.unit_status.message

    @pytest.mark.parametrize("value", [-1, 101])
    def test_invalid_sampling_blocks(
        self, ctx: testing.Context, active_state: testing.State, value: int
    ) -> None:
        """Invalid sampling configuration uses the existing config validation boundary."""
        state = dataclasses.replace(active_state, config={"tracing-sampling-probability": value})
        out = ctx.run(ctx.on.config_changed(), state)
        assert isinstance(out.unit_status, ops.BlockedStatus)


class TestPebbleReadyEvent:
    def test_can_connect(
        self,
        ctx: testing.Context,
        base_state: testing.State,
        mediawiki_container: testing.Container,
    ) -> None:
        """Test that the charm sets the correct status when pebble can connect, but no relations."""
        state_out = ctx.run(ctx.on.pebble_ready(container=mediawiki_container), base_state)
        assert isinstance(state_out.unit_status, ops.BlockedStatus)

    def test_cannot_connect(
        self,
        ctx: testing.Context,
        mediawiki_container: testing.Container,
        shellbox_container: testing.Container,
        secrets: list[testing.Secret],
    ) -> None:
        """Test that the charm sets waiting status when pebble cannot connect."""
        mediawiki_container = dataclasses.replace(mediawiki_container, can_connect=False)

        # With replica secrets
        state_in = testing.State(
            containers=[mediawiki_container, shellbox_container],
            secrets=secrets,
            leader=True,
        )

        state_out = ctx.run(ctx.on.pebble_ready(container=mediawiki_container), state_in)
        assert isinstance(state_out.unit_status, ops.WaitingStatus)

        # Without replica secrets
        state_in = dataclasses.replace(state_in, secrets=[])
        state_out = ctx.run(ctx.on.pebble_ready(container=mediawiki_container), state_in)
        assert isinstance(state_out.unit_status, ops.WaitingStatus)


class TestLeaderElectedEvent:
    @pytest.fixture
    def state(self, base_state: testing.State) -> testing.State:
        """Provides a base state with no secrets."""
        return dataclasses.replace(base_state, secrets=[])

    def test_leader_elected(self, ctx: testing.Context, state: testing.State) -> None:
        """Test that the charm sets up replica data when leader is elected."""
        state_out = ctx.run(ctx.on.leader_elected(), state)
        assert state_out.get_secret(label=Charm._REPLICA_SECRET_LABEL) is not None

    def test_leader_elected_not_leader(self, ctx: testing.Context, state: testing.State) -> None:
        """Test that the charm does not set up replica data when leader is elected but unit is not leader."""
        state_in = dataclasses.replace(state, leader=False)
        state_out = ctx.run(ctx.on.leader_elected(), state_in)
        with pytest.raises(KeyError, match="secret: not found in the State"):
            state_out.get_secret(label=Charm._REPLICA_SECRET_LABEL)

    def test_leader_elected_with_consensus(
        self, ctx: testing.Context, base_state: testing.State
    ) -> None:
        """Test that the charm does not set up replica data when leader is elected but replica consensus has already been reached."""
        expected_secret = copy.deepcopy(
            base_state.get_secret(label=Charm._REPLICA_SECRET_LABEL).latest_content
        )
        state_out = ctx.run(ctx.on.leader_elected(), base_state)
        assert (
            state_out.get_secret(label=Charm._REPLICA_SECRET_LABEL).latest_content
            == expected_secret
        )


class TestConfigChangedEvent:
    def test_valid_config(
        self,
        ctx: testing.Context,
        configured_state: testing.State,
    ) -> None:
        """Test that the charm sets active status when config is changed with all required relations."""
        state_out = ctx.run(ctx.on.config_changed(), configured_state)
        assert isinstance(state_out.unit_status, ops.ActiveStatus)

    def test_invalid_config(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        populated_config: dict[str, bool | float | int | str],
    ) -> None:
        """Test that the charm sets blocked status when config is changed with invalid config."""
        invalid_config = dict(populated_config)
        invalid_config["composer"] = "invalid-json"

        state_in = dataclasses.replace(active_state, config=invalid_config)
        state_out = ctx.run(ctx.on.config_changed(), state_in)
        assert isinstance(state_out.unit_status, ops.BlockedStatus)

    def test_not_ready(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        populated_config: dict[str, bool | float | int | str],
    ) -> None:
        """Test that the charm sets blocked status when config is changed but relations are not ready."""
        state_in = dataclasses.replace(active_state, config=populated_config, relations=[])
        state_out = ctx.run(ctx.on.config_changed(), state_in)
        assert isinstance(state_out.unit_status, ops.BlockedStatus)


class TestTraefikRouteRelationEvents:
    @pytest.fixture
    def state(self, base_state, traefik_route_relation) -> testing.State:
        """Provide a state with a traefik route relation."""
        relations = [traefik_route_relation]
        return dataclasses.replace(base_state, relations=relations)

    def test_relation_joined_without_db(
        self, ctx: testing.Context, state: testing.State, traefik_route_relation: testing.Relation
    ) -> None:
        """Test that the charm sets blocked status when traefik route relation is joined, but without a database relation."""
        state_out = ctx.run(
            ctx.on.relation_joined(relation=traefik_route_relation),
            state,
        )
        assert isinstance(state_out.unit_status, ops.BlockedStatus)

    def test_relation_changed_without_db(
        self, ctx: testing.Context, state: testing.State, traefik_route_relation: testing.Relation
    ) -> None:
        """Test that the charm sets blocked status when traefik route relation is changed, but without a database relation."""
        state_out = ctx.run(
            ctx.on.relation_changed(relation=traefik_route_relation),
            state,
        )
        assert isinstance(state_out.unit_status, ops.BlockedStatus)

    def test_relation_broken_without_db(
        self, ctx: testing.Context, state: testing.State, traefik_route_relation: testing.Relation
    ) -> None:
        """Test that the charm sets blocked status when traefik route relation has departed, but without a database relation."""
        state_out = ctx.run(
            ctx.on.relation_broken(relation=traefik_route_relation),
            state,
        )
        assert isinstance(state_out.unit_status, ops.BlockedStatus)

    def test_relation_joined(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        traefik_route_relation: testing.Relation,
    ) -> None:
        """Test that the charm sets active status when traefik route relation is joined with a database relation."""
        state_out = ctx.run(
            ctx.on.relation_joined(relation=traefik_route_relation),
            active_state,
        )
        assert isinstance(state_out.unit_status, ops.ActiveStatus)

    def test_relation_changed(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        traefik_route_relation: testing.Relation,
    ) -> None:
        """Test that the charm sets active status when traefik route relation is changed with a database relation."""
        state_out = ctx.run(
            ctx.on.relation_changed(relation=traefik_route_relation),
            active_state,
        )
        assert isinstance(state_out.unit_status, ops.ActiveStatus)

    def test_relation_broken(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        traefik_route_relation: testing.Relation,
    ) -> None:
        """Test that the charm sets active status when traefik route relation has departed with a database relation."""
        state_out = ctx.run(
            ctx.on.relation_broken(relation=traefik_route_relation),
            active_state,
        )
        assert isinstance(state_out.unit_status, ops.ActiveStatus)


class TestDatabaseEvents:
    pass


class TestMediaWikiReplicaChanged:
    @pytest.fixture
    def active_state(
        self,
        base_state: testing.State,
        traefik_route_relation: testing.Relation,
        database_relation: testing.Relation,
    ) -> testing.State:
        """Provide a state with all required relations sans mediawiki replica relation, secrets and a pebble ready container."""
        return dataclasses.replace(
            base_state,
            relations=[traefik_route_relation, database_relation],
            planned_units=3,
        )

    def _setup(
        self,
        ctx: testing.Context,
        mediawiki_replica_relation: testing.PeerRelation,
        active_state: testing.State,
        app_mode: OperationMode,
        peer_modes: dict[int, OperationMode | None],
        *,
        unit_mode: OperationMode | None = None,
        leader: bool = True,
    ) -> tuple[testing.PeerRelation, testing.State]:
        """Configure workload modes in the replica relation and build the input state.

        Args:
            ctx: The charm testing context used to load effective configuration.
            mediawiki_replica_relation: The base peer relation fixture to configure.
            active_state: The base state to extend with the configured relation.
            app_mode: The application-requested workload mode.
            peer_modes: Mapping of peer unit ID to its applied mode, or None if absent.
            unit_mode: The local unit's applied mode, or None if absent.
            leader: Whether the local unit should be the leader.

        Returns:
            The configured (replica_relation, state_in) tuple.
        """
        with ctx(ctx.on.update_status(), active_state) as manager:
            state_hash = manager.charm.load_charm_config().state_hash
        peers_data = {
            unit_id: ({MediaWikiPeers.OPERATION_MODE_KEY: mode.value} if mode is not None else {})
            for unit_id, mode in peer_modes.items()
        }
        replace_kwargs: dict = {
            "local_app_data": {
                MediaWikiPeers.OPERATION_MODE_KEY: app_mode.value,
                Charm._COMPOSER_LOCK_KEY: MOCK_COMPOSER_LOCK,
                Charm._COMPOSER_CONFIG_HASH_KEY: state_hash,
            },
            "peers_data": peers_data,
        }
        if unit_mode is not None:
            replace_kwargs["local_unit_data"] = {
                MediaWikiPeers.OPERATION_MODE_KEY: unit_mode.value
            }
        mediawiki_replica_relation = dataclasses.replace(
            mediawiki_replica_relation, **replace_kwargs
        )
        relations = [mediawiki_replica_relation, *active_state.relations]
        state_in = dataclasses.replace(active_state, relations=relations, leader=leader)
        return mediawiki_replica_relation, state_in

    def test_units_become_read_only(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
    ) -> None:
        """Test when the read only database flag is set to true, the unit will also enter read only mode."""
        mediawiki_replica_relation, state_in = self._setup(
            ctx,
            mediawiki_replica_relation,
            active_state,
            app_mode=OperationMode.DATABASE_UPDATE,
            peer_modes={1: None, 2: None},
            leader=False,
        )
        state_out = ctx.run(
            ctx.on.relation_changed(relation=mediawiki_replica_relation, remote_unit=0), state_in
        )
        assert isinstance(state_out.unit_status, ops.MaintenanceStatus)

        out_replica_relation = state_out.get_relation(mediawiki_replica_relation.id)
        assert (
            out_replica_relation.local_app_data[MediaWikiPeers.OPERATION_MODE_KEY]
            == OperationMode.DATABASE_UPDATE.value
        )
        assert (
            out_replica_relation.local_unit_data[MediaWikiPeers.OPERATION_MODE_KEY]
            == OperationMode.DATABASE_UPDATE.value
        )

    def test_leader_waits_for_replicas(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
    ) -> None:
        """Test that the leader unit waits for all replicas to indicate that they are in a read only mode before proceeding with a database update."""
        mediawiki_replica_relation, state_in = self._setup(
            ctx,
            mediawiki_replica_relation,
            active_state,
            app_mode=OperationMode.DATABASE_UPDATE,
            peer_modes={1: OperationMode.DATABASE_UPDATE, 2: None},
        )
        state_out = ctx.run(
            ctx.on.relation_changed(relation=mediawiki_replica_relation, remote_unit=0), state_in
        )
        assert isinstance(state_out.unit_status, ops.WaitingStatus)

        out_replica_relation = state_out.get_relation(mediawiki_replica_relation.id)
        assert (
            out_replica_relation.local_app_data[MediaWikiPeers.OPERATION_MODE_KEY]
            == OperationMode.DATABASE_UPDATE.value
        )
        assert (
            out_replica_relation.local_unit_data[MediaWikiPeers.OPERATION_MODE_KEY]
            == OperationMode.DATABASE_UPDATE.value
        )

    def test_database_update(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
        mock_mediawiki: MockType,
    ) -> None:
        """The leader updates the schema once all units are read-only, then requests normal operation."""
        mediawiki_replica_relation, state_in = self._setup(
            ctx,
            mediawiki_replica_relation,
            active_state,
            app_mode=OperationMode.DATABASE_UPDATE,
            peer_modes={1: OperationMode.DATABASE_UPDATE, 2: OperationMode.DATABASE_UPDATE},
        )
        state_out = ctx.run(
            ctx.on.relation_changed(relation=mediawiki_replica_relation, remote_unit=0), state_in
        )
        assert state_out.unit_status == ops.MaintenanceStatus("Database set to read-only mode")
        mock_mediawiki.update_database_schema.assert_called_once_with()
        mock_mediawiki._settings_reconciliation.assert_not_called()

        out_replica_relation = state_out.get_relation(mediawiki_replica_relation.id)
        assert (
            out_replica_relation.local_app_data[MediaWikiPeers.OPERATION_MODE_KEY]
            == OperationMode.NORMAL.value
        )
        assert (
            out_replica_relation.local_unit_data[MediaWikiPeers.OPERATION_MODE_KEY]
            == OperationMode.DATABASE_UPDATE.value
        )

    def test_leader_acknowledges_normal_operation_on_next_reconciliation(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
    ) -> None:
        """After completing an operation, the leader applies normal operation like any unit."""
        mediawiki_replica_relation, state_in = self._setup(
            ctx,
            mediawiki_replica_relation,
            active_state,
            app_mode=OperationMode.NORMAL,
            peer_modes={1: OperationMode.NORMAL, 2: OperationMode.NORMAL},
            unit_mode=OperationMode.DATABASE_UPDATE,
        )
        state_out = ctx.run(
            ctx.on.relation_changed(relation=mediawiki_replica_relation, remote_unit=0), state_in
        )
        assert state_out.unit_status == ops.ActiveStatus()

        out_replica_relation = state_out.get_relation(mediawiki_replica_relation.id)
        assert (
            out_replica_relation.local_unit_data[MediaWikiPeers.OPERATION_MODE_KEY]
            == OperationMode.NORMAL.value
        )

    def test_leader_does_not_wait_for_replicas_in_normal_mode(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
    ) -> None:
        """Peers that have not acknowledged normal operation do not hold the leader in waiting."""
        mediawiki_replica_relation, state_in = self._setup(
            ctx,
            mediawiki_replica_relation,
            active_state,
            app_mode=OperationMode.NORMAL,
            peer_modes={1: None, 2: OperationMode.DATABASE_UPDATE},
        )
        state_out = ctx.run(
            ctx.on.relation_changed(relation=mediawiki_replica_relation, remote_unit=0), state_in
        )
        assert state_out.unit_status == ops.ActiveStatus()

    def test_only_leader_performs_update(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
    ) -> None:
        """Test that only the leader unit performs the database update when the read only database flag is set, even if all units indicate they are in a read only mode."""
        mediawiki_replica_relation, state_in = self._setup(
            ctx,
            mediawiki_replica_relation,
            active_state,
            app_mode=OperationMode.DATABASE_UPDATE,
            peer_modes={1: OperationMode.DATABASE_UPDATE, 2: OperationMode.DATABASE_UPDATE},
            leader=False,
        )
        state_out = ctx.run(
            ctx.on.relation_changed(relation=mediawiki_replica_relation, remote_unit=0), state_in
        )
        assert isinstance(state_out.unit_status, ops.MaintenanceStatus)

        out_replica_relation = state_out.get_relation(mediawiki_replica_relation.id)
        assert (
            out_replica_relation.local_app_data[MediaWikiPeers.OPERATION_MODE_KEY]
            == OperationMode.DATABASE_UPDATE.value
        )
        assert (
            out_replica_relation.local_unit_data[MediaWikiPeers.OPERATION_MODE_KEY]
            == OperationMode.DATABASE_UPDATE.value
        )

    def test_update_skipped_when_flag_not_set(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
        mock_mediawiki: MockType,
    ) -> None:
        """Test that the database update is skipped if all units indicate they are in a read only mode, but the application level read only database flag is not set."""
        mediawiki_replica_relation, state_in = self._setup(
            ctx,
            mediawiki_replica_relation,
            active_state,
            app_mode=OperationMode.NORMAL,
            peer_modes={1: OperationMode.NORMAL, 2: OperationMode.NORMAL},
            unit_mode=OperationMode.DATABASE_UPDATE,
        )

        mock_mediawiki.update_database_schema.side_effect = MediaWikiInstallError(
            "Mocked install error during database update"
        )

        state_out = ctx.run(
            ctx.on.relation_changed(relation=mediawiki_replica_relation, remote_unit=0), state_in
        )
        assert isinstance(state_out.unit_status, ops.ActiveStatus)

        out_replica_relation = state_out.get_relation(mediawiki_replica_relation.id)
        assert (
            out_replica_relation.local_app_data[MediaWikiPeers.OPERATION_MODE_KEY]
            == OperationMode.NORMAL.value
        )
        assert (
            out_replica_relation.local_unit_data[MediaWikiPeers.OPERATION_MODE_KEY]
            == OperationMode.NORMAL.value
        )

    def test_database_update_failure(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
        mock_mediawiki: MockType,
    ) -> None:
        """Test that should a database update fail, the charm enters an error state and a MediaWikiInstallError exception is raised."""
        mediawiki_replica_relation, state_in = self._setup(
            ctx,
            mediawiki_replica_relation,
            active_state,
            app_mode=OperationMode.DATABASE_UPDATE,
            peer_modes={1: OperationMode.DATABASE_UPDATE, 2: OperationMode.DATABASE_UPDATE},
        )

        mock_mediawiki.update_database_schema.side_effect = MediaWikiInstallError(
            "Mocked install error during database update"
        )

        with pytest.raises(
            testing.errors.UncaughtCharmError, match="Mocked install error during database update"
        ):
            ctx.run(
                ctx.on.relation_changed(relation=mediawiki_replica_relation, remote_unit=0),
                state_in,
            )


class TestReplicaSecrets:
    """Shared authentication token versions survive reconciliation and upgrades."""

    def test_migrate_authentication_token_version(
        self, ctx: testing.Context, active_state: testing.State
    ) -> None:
        """The leader adds the missing field without rotating existing secrets."""
        original = dict(active_state.get_secret(label=Charm._REPLICA_SECRET_LABEL).latest_content)
        original.pop("authentication-token-version")
        legacy_secret = testing.Secret(original, label=Charm._REPLICA_SECRET_LABEL, owner="app")
        state_in = dataclasses.replace(active_state, secrets=[legacy_secret])

        migrated = ctx.run(ctx.on.config_changed(), state_in)
        content = migrated.get_secret(label=Charm._REPLICA_SECRET_LABEL).latest_content
        assert isinstance(migrated.unit_status, ops.ActiveStatus)
        assert len(content["authentication-token-version"]) >= 64
        assert {key: content[key] for key in original} == original

        reconciled = ctx.run(ctx.on.config_changed(), migrated)
        assert reconciled.get_secret(label=Charm._REPLICA_SECRET_LABEL).latest_content == content

        follower = dataclasses.replace(reconciled, leader=False)
        with ctx(ctx.on.config_changed(), follower) as manager:
            peer_state = manager.charm._peers.reconciliation_state()
            assert (
                peer_state.secrets.authentication_token_version
                == content["authentication-token-version"]
            )
            manager.run()

    def test_follower_waits_for_secret_migration(
        self, ctx: testing.Context, active_state: testing.State
    ) -> None:
        """Followers wait for the leader instead of generating independent versions."""
        original = dict(active_state.get_secret(label=Charm._REPLICA_SECRET_LABEL).latest_content)
        original.pop("authentication-token-version")
        legacy_secret = testing.Secret(original, label=Charm._REPLICA_SECRET_LABEL, owner="app")
        state_in = dataclasses.replace(active_state, leader=False, secrets=[legacy_secret])

        state_out = ctx.run(ctx.on.config_changed(), state_in)

        assert state_out.unit_status == ops.WaitingStatus(
            "Waiting for leader to migrate replica secrets"
        )
        assert state_out.get_secret(label=Charm._REPLICA_SECRET_LABEL).latest_content == original


class TestRotateMediaWikiSecretsAction:
    def test_success(
        self, ctx: testing.Context, active_state: testing.State, mocker: MockerFixture
    ) -> None:
        """Test that rotate-mediawiki-secrets updates replica secret content."""
        expected_secret_content = {
            "key": "new-mocked-key",
            "session": "new-mocked-session",
            "saml-salt": "new-mocked-saml-salt",
            "authentication-token-version": "new-mocked-authentication-token-version",
        }
        mocker.patch(
            "mediawiki_peers.MediaWikiSecrets.generate",
            return_value=MediaWikiSecrets.from_juju_secret(expected_secret_content),
        )

        state_out = ctx.run(ctx.on.action("rotate-mediawiki-secrets"), active_state)

        assert (
            state_out.get_secret(label=Charm._REPLICA_SECRET_LABEL).latest_content
            == expected_secret_content
        )
        assert ctx.action_results is None

    def test_not_leader(self, ctx: testing.Context, active_state: testing.State) -> None:
        """Test that rotate-mediawiki-secrets fails when the unit is not leader."""
        state_in = dataclasses.replace(active_state, leader=False)

        with pytest.raises(
            testing.ActionFailed, match="Only the leader unit can rotate MediaWiki secrets"
        ):
            ctx.run(ctx.on.action("rotate-mediawiki-secrets"), state_in)

        assert ctx.action_results is None

    def test_secret_not_found(self, ctx: testing.Context, active_state: testing.State) -> None:
        """Test that rotate-mediawiki-secrets fails when replica secret is missing."""
        state_in = dataclasses.replace(active_state, secrets=[])

        with pytest.raises(testing.ActionFailed, match="replica secret not found"):
            ctx.run(ctx.on.action("rotate-mediawiki-secrets"), state_in)

        assert ctx.action_results is None

    def test_unexpected_error(
        self, ctx: testing.Context, active_state: testing.State, mocker: MockerFixture
    ) -> None:
        """Test that rotate-mediawiki-secrets fails on unexpected exceptions."""
        mocker.patch(
            "mediawiki_peers.MediaWikiSecrets.generate", side_effect=Exception("Mocked exception")
        )

        with pytest.raises(
            testing.ActionFailed, match="Failed to rotate secrets due to unexpected error"
        ):
            ctx.run(ctx.on.action("rotate-mediawiki-secrets"), active_state)

        assert ctx.action_results is None


class TestCreateAndPromoteUserAction:
    def test_success_generated_password(
        self, ctx: testing.Context, active_state: testing.State, mock_mediawiki
    ) -> None:
        """Test that a generated password is returned and forwarded when requested."""
        ctx.run(
            ctx.on.action(
                "create-and-promote",
                params={"username": "mocked-user", "bureaucrat": True, "generate-password": True},
            ),
            active_state,
        )

        # no sec B105 is bugged for multi-line dicts https://github.com/PyCQA/bandit/issues/1352
        mocked_password = "mocked-password"  # nosec: B105
        assert ctx.action_results == {"username": "mocked-user", "password": mocked_password}
        mock_mediawiki.create_and_promote_user.assert_called_once_with(
            "mocked-user",
            generate_password=True,
            sysop=False,
            bureaucrat=True,
            interface_admin=False,
            bot=False,
            force=False,
            custom_groups=None,
            email=None,
            reason=None,
        )

    def test_success_no_password(
        self, ctx: testing.Context, active_state: testing.State, mock_mediawiki
    ) -> None:
        """Test that no password is returned when generate-password is disabled."""
        mock_mediawiki.create_and_promote_user.return_value = None
        ctx.run(
            ctx.on.action(
                "create-and-promote",
                params={
                    "username": "mocked-user",
                    "generate-password": False,
                    "force": True,
                    "sysop": True,
                    "email": "user@example.com",
                    "reason": "bootstrap",
                },
            ),
            active_state,
        )

        assert ctx.action_results == {"username": "mocked-user"}
        mock_mediawiki.create_and_promote_user.assert_called_once_with(
            "mocked-user",
            generate_password=False,
            sysop=True,
            bureaucrat=False,
            interface_admin=False,
            bot=False,
            force=True,
            custom_groups=None,
            email="user@example.com",
            reason="bootstrap",
        )

    def test_no_password_without_force_fails(
        self, ctx: testing.Context, active_state: testing.State, mock_mediawiki
    ) -> None:
        """Test that disabling password generation requires force."""
        with pytest.raises(testing.ActionFailed, match="Refusing to create a user without"):
            ctx.run(
                ctx.on.action(
                    "create-and-promote",
                    params={"username": "mocked-user", "generate-password": False},
                ),
                active_state,
            )

        mock_mediawiki.create_and_promote_user.assert_not_called()

    def test_failure(
        self, ctx: testing.Context, active_state: testing.State, mock_mediawiki
    ) -> None:
        """Test that the action fails when MediaWiki raises an exception during user creation."""
        mock_mediawiki.create_and_promote_user.side_effect = MediaWikiBlockedStatusException(
            "Mocked blocked status during user creation"
        )

        with pytest.raises(
            testing.ActionFailed, match="Mocked blocked status during user creation"
        ):
            ctx.run(
                ctx.on.action(
                    "create-and-promote",
                    params={"username": "mocked-user", "generate-password": True},
                ),
                active_state,
            )

        assert ctx.action_results is None

        mock_mediawiki.create_and_promote_user.side_effect = Exception(
            "Mocked exception during user creation"
        )

        with pytest.raises(testing.ActionFailed, match="User creation failed"):
            ctx.run(
                ctx.on.action(
                    "create-and-promote",
                    params={"username": "mocked-user", "generate-password": True},
                ),
                active_state,
            )

        assert ctx.action_results is None

    def test_failure_surfaces_stderr(
        self, ctx: testing.Context, active_state: testing.State, mock_mediawiki
    ) -> None:
        """Test that the script's stderr is surfaced in the action failure message."""
        mock_mediawiki.create_and_promote_user.side_effect = MediaWikiInstallError(
            "Creating user failed: Account exists already"
        )

        with pytest.raises(
            testing.ActionFailed,
            match="User creation failed: Creating user failed: Account exists already",
        ):
            ctx.run(
                ctx.on.action(
                    "create-and-promote",
                    params={"username": "mocked-user", "generate-password": True},
                ),
                active_state,
            )

        assert ctx.action_results is None


class TestUpdateDatabaseAction:
    def test_success(self, ctx: testing.Context, active_state: testing.State) -> None:
        """Test that the action completes successfully when database update is successful."""
        ctx.run(ctx.on.action("update-database"), active_state)
        assert ctx.action_results is None

    def test_mediawiki_replica_not_ready(
        self, ctx: testing.Context, base_state: testing.State
    ) -> None:
        """Test that the action fails when the mediawiki-replica relation is not ready."""
        with pytest.raises(testing.ActionFailed, match="Peer relation not ready yet"):
            ctx.run(ctx.on.action("update-database"), base_state)

        assert ctx.action_results is None

    def test_not_leader(self, ctx: testing.Context, active_state: testing.State) -> None:
        """Test that the action fails when the unit is not the leader."""
        state_in = dataclasses.replace(active_state, leader=False)
        with pytest.raises(
            testing.ActionFailed, match="Only the leader unit can request a database update"
        ):
            ctx.run(ctx.on.action("update-database"), state_in)

        assert ctx.action_results is None

    def test_rejected_during_maintenance(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
    ) -> None:
        """Test that a database update cannot begin during maintenance mode."""
        relation = dataclasses.replace(
            mediawiki_replica_relation,
            local_app_data={MediaWikiPeers.OPERATION_MODE_KEY: OperationMode.MAINTENANCE.value},
        )
        state_in = dataclasses.replace(
            active_state,
            relations=[
                relation,
                *(item for item in active_state.relations if item.id != relation.id),
            ],
        )

        with pytest.raises(
            testing.ActionFailed,
            match="Disable maintenance mode before requesting a database update",
        ):
            ctx.run(ctx.on.action("update-database"), state_in)


class TestSetMaintenanceModeAction:
    """Tests for the set-maintenance-mode action."""

    @pytest.fixture(autouse=True)
    def set_database_read_only(self, mocker: MockerFixture) -> MockType:
        """Isolate database operations while exercising real action coordination."""
        return mocker.patch("relations.database.Database.set_read_only", autospec=True)

    @pytest.fixture(autouse=True)
    def database_relation_ready(self, mocker: MockerFixture) -> MockType:
        """Treat the database relation as ready unless a test overrides it."""
        return mocker.patch(
            "relations.database.Database.is_relation_ready", autospec=True, return_value=True
        )

    @pytest.mark.parametrize(
        "error",
        [
            MediaWikiWaitingStatusException("Waiting for pebble"),
            MediaWikiBlockedStatusException("Database relation is not ready"),
            MediaWikiWaitingStatusException("Waiting for MediaWiki to be installed"),
        ],
    )
    def test_enable_workload_not_ready(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
        mock_mediawiki: MockType,
        set_database_read_only: MockType,
        error: Exception,
    ) -> None:
        """Enabling maintenance mode fails before touching the database or peer state."""
        mock_mediawiki.ensure_ready.side_effect = error
        with pytest.raises(testing.ActionFailed, match=str(error)):
            ctx.run(
                ctx.on.action("set-maintenance-mode", params={"enabled": True}),
                active_state,
            )
        set_database_read_only.assert_not_called()

    @pytest.mark.parametrize(
        "error",
        [
            MediaWikiWaitingStatusException("Waiting for pebble"),
            MediaWikiWaitingStatusException("Waiting for MediaWiki to be installed"),
        ],
    )
    def test_disable_when_workload_not_ready(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
        mock_mediawiki: MockType,
        set_database_read_only: MockType,
        error: Exception,
    ) -> None:
        """Disabling maintenance mode only needs the database, so an uninstalled wiki can recover."""
        mock_mediawiki.ensure_ready.side_effect = error
        relation = dataclasses.replace(
            mediawiki_replica_relation,
            local_app_data={
                MediaWikiPeers.OPERATION_MODE_KEY: OperationMode.MAINTENANCE.value,
            },
        )
        state_in = dataclasses.replace(
            active_state,
            relations=[
                relation,
                *(item for item in active_state.relations if item.id != relation.id),
            ],
        )

        state_out = ctx.run(
            ctx.on.action("set-maintenance-mode", params={"enabled": False}),
            state_in,
        )

        app_data = state_out.get_relation(relation.id).local_app_data
        assert app_data[MediaWikiPeers.OPERATION_MODE_KEY] == OperationMode.NORMAL.value
        assert set_database_read_only.call_args.args[1] is False

    def test_disable_database_relation_not_ready(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
        database_relation_ready: MockType,
        set_database_read_only: MockType,
    ) -> None:
        """Disabling maintenance mode fails before touching peer state without a database."""
        database_relation_ready.return_value = False
        with pytest.raises(testing.ActionFailed, match="Database relation is not ready"):
            ctx.run(
                ctx.on.action("set-maintenance-mode", params={"enabled": False}),
                active_state,
            )
        set_database_read_only.assert_not_called()

    def test_enable_with_message(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
        set_database_read_only: MockType,
    ) -> None:
        """The leader records maintenance intent and its user-facing message."""
        state_out = ctx.run(
            ctx.on.action(
                "set-maintenance-mode",
                params={"enabled": True, "message": "Database migration"},
            ),
            active_state,
        )

        app_data = state_out.get_relation(mediawiki_replica_relation.id).local_app_data
        assert app_data[MediaWikiPeers.OPERATION_MODE_KEY] == OperationMode.MAINTENANCE.value
        assert app_data[MediaWikiPeers.MAINTENANCE_MESSAGE_KEY] == "Database migration"
        assert set_database_read_only.call_args.args[1] is True

    def test_enable_without_message(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
    ) -> None:
        """Without a message, no message is recorded; Juju supplies the action default."""
        state_out = ctx.run(
            ctx.on.action("set-maintenance-mode", params={"enabled": True}),
            active_state,
        )

        app_data = state_out.get_relation(mediawiki_replica_relation.id).local_app_data
        assert app_data[MediaWikiPeers.OPERATION_MODE_KEY] == OperationMode.MAINTENANCE.value
        assert MediaWikiPeers.MAINTENANCE_MESSAGE_KEY not in app_data

    def test_enable_with_empty_message(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
    ) -> None:
        """An empty message is accepted and represented by an absent peer-data key."""
        state_out = ctx.run(
            ctx.on.action(
                "set-maintenance-mode",
                params={"enabled": True, "message": ""},
            ),
            active_state,
        )

        app_data = state_out.get_relation(mediawiki_replica_relation.id).local_app_data
        assert MediaWikiPeers.MAINTENANCE_MESSAGE_KEY not in app_data

    def test_without_enabled_reports_current_state_without_changes(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
        set_database_read_only: MockType,
    ) -> None:
        """Omitting enabled reports the current state without modifying peer data."""
        relation = dataclasses.replace(
            mediawiki_replica_relation,
            local_app_data={
                MediaWikiPeers.OPERATION_MODE_KEY: OperationMode.MAINTENANCE.value,
                MediaWikiPeers.MAINTENANCE_MESSAGE_KEY: "Database migration",
            },
        )
        state_in = dataclasses.replace(
            active_state,
            relations=[
                relation,
                *(item for item in active_state.relations if item.id != relation.id),
            ],
        )

        state_out = ctx.run(ctx.on.action("set-maintenance-mode"), state_in)

        assert ctx.action_results == {"enabled": True, "message": "Database migration"}
        assert state_out.get_relation(relation.id).local_app_data == relation.local_app_data
        set_database_read_only.assert_not_called()

    def test_disable_clears_message(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
        set_database_read_only: MockType,
    ) -> None:
        """Disabling maintenance clears the previous message."""
        relation = dataclasses.replace(
            mediawiki_replica_relation,
            local_app_data={
                MediaWikiPeers.OPERATION_MODE_KEY: OperationMode.MAINTENANCE.value,
                MediaWikiPeers.MAINTENANCE_MESSAGE_KEY: "Database migration",
            },
        )
        state_in = dataclasses.replace(
            active_state,
            relations=[
                relation,
                *(item for item in active_state.relations if item.id != relation.id),
            ],
        )

        state_out = ctx.run(
            ctx.on.action("set-maintenance-mode", params={"enabled": False}),
            state_in,
        )

        app_data = state_out.get_relation(relation.id).local_app_data
        assert app_data[MediaWikiPeers.OPERATION_MODE_KEY] == OperationMode.NORMAL.value
        assert MediaWikiPeers.MAINTENANCE_MESSAGE_KEY not in app_data
        assert set_database_read_only.call_args.args[1] is False

    def test_not_leader(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        set_database_read_only: MockType,
    ) -> None:
        """A non-leader cannot change application-wide maintenance mode."""
        state_in = dataclasses.replace(active_state, leader=False)
        with pytest.raises(
            testing.ActionFailed,
            match="Only the leader unit can set maintenance mode",
        ):
            ctx.run(
                ctx.on.action("set-maintenance-mode", params={"enabled": True}),
                state_in,
            )
        set_database_read_only.assert_not_called()

    def test_peer_relation_not_ready(
        self,
        ctx: testing.Context,
        base_state: testing.State,
        set_database_read_only: MockType,
    ) -> None:
        """The action fails before persisting state when peers are unavailable."""
        with pytest.raises(testing.ActionFailed, match="Peer relation not ready yet"):
            ctx.run(
                ctx.on.action("set-maintenance-mode", params={"enabled": True}),
                base_state,
            )
        set_database_read_only.assert_not_called()

    @pytest.mark.parametrize(
        ("mode", "message"),
        [
            (
                OperationMode.DATABASE_UPDATE,
                "Wait for the database update to complete before {action} maintenance mode",
            ),
            (
                OperationMode.FORCE_RECONCILIATION,
                "Wait for forced reconciliation to complete before {action} maintenance mode",
            ),
        ],
    )
    @pytest.mark.parametrize(("enabled", "action"), [(True, "enabling"), (False, "disabling")])
    def test_rejected_during_conflicting_operation(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
        set_database_read_only: MockType,
        mode: OperationMode,
        message: str,
        enabled: bool,
        action: str,
    ) -> None:
        """Conflicting operations reject maintenance changes before touching the database."""
        relation = dataclasses.replace(
            mediawiki_replica_relation,
            local_app_data={MediaWikiPeers.OPERATION_MODE_KEY: mode.value},
        )
        state_in = dataclasses.replace(
            active_state,
            relations=[
                relation,
                *(item for item in active_state.relations if item.id != relation.id),
            ],
        )

        with pytest.raises(
            testing.ActionFailed,
            match=message.format(action=action),
        ):
            ctx.run(
                ctx.on.action("set-maintenance-mode", params={"enabled": enabled}),
                state_in,
            )
        set_database_read_only.assert_not_called()

    @pytest.mark.parametrize(
        ("mode", "message"),
        [
            (
                OperationMode.MAINTENANCE,
                "Disable maintenance mode before starting another operation",
            ),
            (
                OperationMode.DATABASE_UPDATE,
                "Wait for all units to return to normal operation before starting another operation",
            ),
            (
                OperationMode.FORCE_RECONCILIATION,
                "Wait for all units to return to normal operation before starting another operation",
            ),
        ],
    )
    def test_enable_rejected_until_units_are_normal(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
        set_database_read_only: MockType,
        mode: OperationMode,
        message: str,
    ) -> None:
        """Pending unit transitions are checked before making the schema read-only."""
        relation = dataclasses.replace(
            mediawiki_replica_relation,
            peers_data={1: {MediaWikiPeers.OPERATION_MODE_KEY: mode.value}},
        )
        state = dataclasses.replace(
            active_state,
            relations=[
                relation,
                *(item for item in active_state.relations if item.id != relation.id),
            ],
        )
        with pytest.raises(testing.ActionFailed, match=message):
            ctx.run(ctx.on.action("set-maintenance-mode", params={"enabled": True}), state)
        set_database_read_only.assert_not_called()

    @pytest.mark.parametrize("enabled", [True, False])
    def test_repeated_request_still_verifies_database(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
        set_database_read_only: MockType,
        enabled: bool,
    ) -> None:
        """Retries verify database protection even when peer intent already matches."""
        mode = OperationMode.MAINTENANCE if enabled else OperationMode.NORMAL
        relation = dataclasses.replace(
            mediawiki_replica_relation,
            local_app_data={MediaWikiPeers.OPERATION_MODE_KEY: mode.value},
        )
        state = dataclasses.replace(
            active_state,
            relations=[
                relation,
                *(item for item in active_state.relations if item.id != relation.id),
            ],
        )
        ctx.run(ctx.on.action("set-maintenance-mode", params={"enabled": enabled}), state)
        assert set_database_read_only.call_count == 1
        assert set_database_read_only.call_args.args[1] is enabled

    @pytest.mark.parametrize("enabled", [True, False])
    @pytest.mark.parametrize(
        "error",
        [
            MediaWikiBlockedStatusException("Unable to set database read-only mode: denied"),
            MediaWikiWaitingStatusException("Database relation is not ready"),
            MediaWikiBlockedStatusException("Unable to verify database read-only state"),
        ],
    )
    def test_database_failure_leaves_peer_state_unchanged(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
        set_database_read_only: MockType,
        mock_mediawiki: MockType,
        enabled: bool,
        error: Exception,
    ) -> None:
        """Database failures fail the action without publishing intent or reconciling."""
        relation = dataclasses.replace(
            mediawiki_replica_relation,
            local_app_data={
                MediaWikiPeers.OPERATION_MODE_KEY: (
                    OperationMode.NORMAL.value if enabled else OperationMode.MAINTENANCE.value
                ),
                MediaWikiPeers.MAINTENANCE_MESSAGE_KEY: "Previous message",
            },
        )
        state = dataclasses.replace(
            active_state,
            relations=[
                relation,
                *(item for item in active_state.relations if item.id != relation.id),
            ],
        )
        set_database_read_only.side_effect = error
        with ctx(ctx.on.action("set-maintenance-mode", params={"enabled": enabled}), state) as mgr:
            with pytest.raises(
                testing.ActionFailed, match="Failed to set database read-only mode"
            ):
                mgr.run()
            peer = mgr.charm.model.get_relation(relation.endpoint)
            assert peer is not None
            assert dict(peer.data[mgr.charm.app]) == relation.local_app_data
        assert set_database_read_only.call_count == 1
        mock_mediawiki.reconciliation.assert_not_called()

    @pytest.mark.parametrize("enabled", [True, False])
    def test_database_change_precedes_publishing_intent(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
        set_database_read_only: MockType,
        mock_mediawiki: MockType,
        enabled: bool,
    ) -> None:
        """The database callback completes before either direction changes peer mode."""
        previous_mode = OperationMode.NORMAL if enabled else OperationMode.MAINTENANCE
        relation = dataclasses.replace(
            mediawiki_replica_relation,
            local_app_data={MediaWikiPeers.OPERATION_MODE_KEY: previous_mode.value},
        )
        state = dataclasses.replace(
            active_state,
            relations=[
                relation,
                *(item for item in active_state.relations if item.id != relation.id),
            ],
        )
        with ctx(ctx.on.action("set-maintenance-mode", params={"enabled": enabled}), state) as mgr:

            def check_previous_mode(*args: Any) -> None:
                """Inspect peer intent at the point the synchronous operation executes."""
                peer = mgr.charm.model.get_relation(relation.endpoint)
                assert peer is not None
                assert (
                    peer.data[mgr.charm.app][MediaWikiPeers.OPERATION_MODE_KEY]
                    == previous_mode.value
                )

            set_database_read_only.side_effect = check_previous_mode
            state_out = mgr.run()
        expected_mode = OperationMode.MAINTENANCE if enabled else OperationMode.NORMAL
        assert (
            state_out.get_relation(relation.id).local_app_data[MediaWikiPeers.OPERATION_MODE_KEY]
            == expected_mode.value
        )
        assert set_database_read_only.call_count == 1
        mock_mediawiki.reconciliation.assert_not_called()


class TestForceReconciliationAction:
    """Tests for the force-reconciliation action."""

    def test_runs_forced_reconciliation(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mock_mediawiki: MockType,
    ) -> None:
        """Test that the action runs reconciliation with forced composer update."""
        ctx.run(ctx.on.action("force-reconciliation"), active_state)
        mock_mediawiki.reconciliation.assert_called_once()
        call_kwargs = mock_mediawiki.reconciliation.call_args.kwargs
        assert call_kwargs.get("force") is True

    def test_non_leader_runs_forced_reconciliation(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
        mock_mediawiki: MockType,
    ) -> None:
        """Test that a non-leader can run a forced reconciliation on itself."""
        with ctx(ctx.on.update_status(), active_state) as manager:
            state_hash = manager.charm.load_charm_config().state_hash
        # Non-leaders require lock data in peer relation to proceed past the composer gate.
        lock_rel = dataclasses.replace(
            mediawiki_replica_relation,
            local_app_data={
                Charm._COMPOSER_LOCK_KEY: MOCK_COMPOSER_LOCK,
                Charm._COMPOSER_CONFIG_HASH_KEY: state_hash,
            },
        )
        relations_without_peer = [
            r for r in active_state.relations if r.endpoint != Charm._PEER_RELATION_NAME
        ]
        state_in = dataclasses.replace(
            active_state,
            leader=False,
            relations=[*relations_without_peer, lock_rel],
        )
        ctx.run(ctx.on.action("force-reconciliation"), state_in)
        mock_mediawiki.reconciliation.assert_called_once()
        call_kwargs = mock_mediawiki.reconciliation.call_args.kwargs
        assert call_kwargs.get("force") is True

    def test_all_units_sets_operation_mode(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
        mock_mediawiki: MockType,
    ) -> None:
        """Test that all-units=true requests forced reconciliation as an operation mode."""
        state_out = ctx.run(
            ctx.on.action("force-reconciliation", params={"all-units": True}), active_state
        )
        replica_relation = state_out.get_relation(mediawiki_replica_relation.id)
        assert (
            replica_relation.local_app_data[MediaWikiPeers.OPERATION_MODE_KEY]
            == OperationMode.FORCE_RECONCILIATION.value
        )
        mock_mediawiki.reconciliation.assert_not_called()

    def test_all_units_operation_clears_after_reconciliation(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
        mock_mediawiki: MockType,
    ) -> None:
        """Forced reconciliation requests normal operation after completion."""
        relation = dataclasses.replace(
            mediawiki_replica_relation,
            local_app_data={
                MediaWikiPeers.OPERATION_MODE_KEY: OperationMode.FORCE_RECONCILIATION.value,
            },
        )
        state_in = dataclasses.replace(
            active_state,
            relations=[
                relation,
                *(item for item in active_state.relations if item.id != relation.id),
            ],
        )

        state_out = ctx.run(ctx.on.config_changed(), state_in)

        replica_relation = state_out.get_relation(relation.id)
        assert (
            replica_relation.local_app_data[MediaWikiPeers.OPERATION_MODE_KEY]
            == OperationMode.NORMAL.value
        )
        assert (
            replica_relation.local_unit_data[MediaWikiPeers.OPERATION_MODE_KEY]
            == OperationMode.FORCE_RECONCILIATION.value
        )
        peer_state = mock_mediawiki._reconcile_configuration.call_args.args[0]
        assert peer_state.operation_mode is OperationMode.FORCE_RECONCILIATION

    def test_all_units_rejected_during_maintenance(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
    ) -> None:
        """Test that forced reconciliation cannot overlap maintenance mode."""
        relation = dataclasses.replace(
            mediawiki_replica_relation,
            local_app_data={MediaWikiPeers.OPERATION_MODE_KEY: OperationMode.MAINTENANCE.value},
        )
        state_in = dataclasses.replace(
            active_state,
            relations=[
                relation,
                *(item for item in active_state.relations if item.id != relation.id),
            ],
        )

        with pytest.raises(
            testing.ActionFailed,
            match="Wait for maintenance to complete before forcing reconciliation",
        ):
            ctx.run(
                ctx.on.action("force-reconciliation", params={"all-units": True}),
                state_in,
            )

    def test_all_units_rejected_while_units_return_to_normal(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
    ) -> None:
        """A new operation cannot reuse acknowledgments from the previous operation."""
        relation = dataclasses.replace(
            mediawiki_replica_relation,
            local_app_data={MediaWikiPeers.OPERATION_MODE_KEY: OperationMode.NORMAL.value},
            local_unit_data={
                MediaWikiPeers.OPERATION_MODE_KEY: OperationMode.FORCE_RECONCILIATION.value
            },
            peers_data={
                1: {MediaWikiPeers.OPERATION_MODE_KEY: OperationMode.FORCE_RECONCILIATION.value}
            },
        )
        state_in = dataclasses.replace(
            active_state,
            relations=[
                relation,
                *(item for item in active_state.relations if item.id != relation.id),
            ],
        )

        with pytest.raises(
            testing.ActionFailed,
            match="Wait for all units to return to normal operation",
        ):
            ctx.run(
                ctx.on.action("force-reconciliation", params={"all-units": True}),
                state_in,
            )

    def test_all_units_not_leader(self, ctx: testing.Context, active_state: testing.State) -> None:
        """Test that all-units=true fails when not the leader."""
        state_in = dataclasses.replace(active_state, leader=False)
        with pytest.raises(
            testing.ActionFailed,
            match="The all-units flag requires the action to be run on the leader unit",
        ):
            ctx.run(ctx.on.action("force-reconciliation", params={"all-units": True}), state_in)

    def test_all_units_peer_relation_not_ready(
        self, ctx: testing.Context, base_state: testing.State
    ) -> None:
        """Test that all-units=true fails when peer relation is not ready."""
        with pytest.raises(testing.ActionFailed, match="Peer relation not ready yet"):
            ctx.run(ctx.on.action("force-reconciliation", params={"all-units": True}), base_state)


class TestUnknownOperationMode:
    """Tests for application data holding an operation mode this charm does not recognise."""

    @pytest.fixture
    def state_in(
        self, active_state: testing.State, mediawiki_replica_relation: testing.PeerRelation
    ) -> testing.State:
        """Provide an active state whose peer application data holds an unknown mode."""
        relation = dataclasses.replace(
            mediawiki_replica_relation,
            local_app_data={
                MediaWikiPeers.OPERATION_MODE_KEY: "future-mode",
                MediaWikiPeers.MAINTENANCE_MESSAGE_KEY: "Previous message",
            },
            local_unit_data={MediaWikiPeers.OPERATION_MODE_KEY: "future-mode"},
            peers_data={1: {MediaWikiPeers.OPERATION_MODE_KEY: "future-mode"}},
        )
        return dataclasses.replace(
            active_state,
            relations=[
                relation,
                *(item for item in active_state.relations if item.id != relation.id),
            ],
        )

    def test_reconciliation_blocks_without_changing_workload(
        self,
        ctx: testing.Context,
        state_in: testing.State,
        mock_mediawiki: MockType,
        mocker: MockerFixture,
    ) -> None:
        """The unit is blocked and leaves its services as they are."""
        reconcile_services = mocker.patch.object(WorkloadMediaWiki, "_reconcile_services")

        state_out = ctx.run(ctx.on.config_changed(), state_in)

        assert state_out.unit_status == ops.BlockedStatus(
            "Unknown operation mode 'future-mode', run set-maintenance-mode to override it"
        )
        reconcile_services.assert_not_called()
        mock_mediawiki._reconcile_configuration.assert_not_called()

    @pytest.mark.parametrize(
        ("action", "params"),
        [
            ("update-database", {}),
            ("set-maintenance-mode", {}),
            ("force-reconciliation", {"all-units": True}),
        ],
    )
    def test_actions_fail(
        self,
        ctx: testing.Context,
        state_in: testing.State,
        mocker: MockerFixture,
        action: str,
        params: dict[str, bool],
    ) -> None:
        """Actions that read the operation mode fail with the unknown value."""
        set_database_read_only = mocker.patch(
            "relations.database.Database.set_read_only", autospec=True
        )
        with pytest.raises(testing.ActionFailed, match="Unknown operation mode 'future-mode'"):
            ctx.run(ctx.on.action(action, params=params), state_in)
        set_database_read_only.assert_not_called()

    @pytest.mark.parametrize(
        ("enabled", "expected_mode"),
        [(True, OperationMode.MAINTENANCE), (False, OperationMode.NORMAL)],
    )
    def test_set_maintenance_mode_overrides(
        self,
        ctx: testing.Context,
        state_in: testing.State,
        mocker: MockerFixture,
        enabled: bool,
        expected_mode: OperationMode,
    ) -> None:
        """Setting maintenance mode replaces the unknown mode and applies the database state."""
        set_database_read_only = mocker.patch(
            "relations.database.Database.set_read_only", autospec=True
        )
        mocker.patch(
            "relations.database.Database.is_relation_ready", autospec=True, return_value=True
        )

        state_out = ctx.run(
            ctx.on.action(
                "set-maintenance-mode", params={"enabled": enabled, "message": "Recovering"}
            ),
            state_in,
        )

        relation = state_out.get_relation(
            next(r.id for r in state_in.relations if r.endpoint == "mediawiki-replica")
        )
        assert relation.local_app_data[MediaWikiPeers.OPERATION_MODE_KEY] == expected_mode.value
        assert relation.local_app_data.get(MediaWikiPeers.MAINTENANCE_MESSAGE_KEY) == (
            "Recovering" if enabled else None
        )
        assert set_database_read_only.call_args.args[1] is enabled


class TestRunMaintenanceScriptAction:
    """Tests for the run-maintenance-script action."""

    @pytest.mark.parametrize(
        "script",
        [
            "checkImages",
            "cleanupBlocks",
            "cleanupEmptyCategories",
            "cleanupInvalidDbKeys",
            "cleanupPreferences",
            "cleanupUploadStash",
            "cleanupWatchlist",
            "findMissingFiles",
            "findOrphanedFiles",
            "pruneFileCache",
            "purgeChangedFiles",
            "purgeChangedPages",
            "purgeExpiredBlocks",
            "purgeExpiredUserrights",
            "purgeExpiredWatchlistItems",
            "purgeMessageBlobStore",
            "purgeParserCache",
            "rebuildall",
            "rebuildImages",
            "rebuildrecentchanges",
            "rebuildtextindex",
            "recountCategories",
            "refreshImageMetadata",
            "refreshLinks",
            "removeUnusedAccounts",
            "resetPageRandom",
            "showJobs",
            "showSiteStats",
            "updateArticleCount",
        ],
    )
    def test_success(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mock_mediawiki: MockType,
        script: str,
    ) -> None:
        """Test that the action succeeds and returns stdout as results."""
        ctx.run(
            ctx.on.action("run-maintenance-script", params={"script": script}),
            active_state,
        )
        assert ctx.action_results == {"output": "mocked-output\n"}
        mock_mediawiki.run_maintenance_script.assert_called_once_with([script])

    def test_success_with_args(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mock_mediawiki: MockType,
    ) -> None:
        """Test that extra args are parsed and forwarded to the maintenance script."""
        ctx.run(
            ctx.on.action(
                "run-maintenance-script",
                params={
                    "script": "resetPageRandom",
                    "args": "--from 20000101000000 --to 21000101000000 --dry",
                },
            ),
            active_state,
        )
        assert ctx.action_results == {"output": "mocked-output\n"}
        mock_mediawiki.run_maintenance_script.assert_called_once_with(
            [
                "resetPageRandom",
                "--from",
                "20000101000000",
                "--to",
                "21000101000000",
                "--dry",
            ]
        )

    def test_success_output_only(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mock_mediawiki: MockType,
    ) -> None:
        """Test that only stdout is returned; stderr is not included in results."""
        mock_mediawiki.run_maintenance_script.return_value = CommandExecResult(
            return_code=0, stdout="some output\n", stderr="a warning\nanother warning\n"
        )
        ctx.run(
            ctx.on.action("run-maintenance-script", params={"script": "showJobs"}),
            active_state,
        )
        assert ctx.action_results == {"output": "some output\n"}

    @pytest.mark.parametrize(
        "script",
        ["getConfiguration", "install", "mysql", "update"],
    )
    def test_non_allowed_script(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mock_mediawiki: MockType,
        script: str,
    ) -> None:
        """Test that scripts outside the allow list are rejected with a clear error."""
        with pytest.raises(
            testing.ActionFailed, match=f"'{script}' is not an allowed maintenance script"
        ):
            ctx.run(
                ctx.on.action("run-maintenance-script", params={"script": script}),
                active_state,
            )
        mock_mediawiki.run_maintenance_script.assert_not_called()

    @pytest.mark.parametrize(
        "args,flag",
        [
            ("--conf=/etc/evil.php", "--conf"),
            ("--conf /etc/evil.php", "--conf"),
        ],
    )
    def test_blocked_flag(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mock_mediawiki: MockType,
        args: str,
        flag: str,
    ) -> None:
        """Test that blocked flags are rejected with a clear error."""
        with pytest.raises(testing.ActionFailed, match=f"Flag '{flag}' is not permitted"):
            ctx.run(
                ctx.on.action(
                    "run-maintenance-script",
                    params={"script": "showJobs", "args": args},
                ),
                active_state,
            )
        mock_mediawiki.run_maintenance_script.assert_not_called()

    def test_absolute_path_script(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mock_mediawiki: MockType,
    ) -> None:
        """Test that scripts starting with '/' are rejected."""
        with pytest.raises(testing.ActionFailed, match="Invalid script name"):
            ctx.run(
                ctx.on.action("run-maintenance-script", params={"script": "/etc/passwd"}),
                active_state,
            )
        mock_mediawiki.run_maintenance_script.assert_not_called()

    def test_path_traversal_script(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mock_mediawiki: MockType,
    ) -> None:
        """Test that scripts containing '..' path components are rejected."""
        with pytest.raises(testing.ActionFailed, match="Invalid script name"):
            ctx.run(
                ctx.on.action("run-maintenance-script", params={"script": "../../etc/passwd"}),
                active_state,
            )
        mock_mediawiki.run_maintenance_script.assert_not_called()

    def test_script_failure(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mock_mediawiki: MockType,
    ) -> None:
        """Test that a non-zero exit code surfaces stdout and stderr in the failure message."""
        mock_mediawiki.run_maintenance_script.return_value = CommandExecResult(
            return_code=1, stdout="progress output\n", stderr="fatal: something went wrong\n"
        )
        with pytest.raises(
            testing.ActionFailed,
            match="Maintenance script failed \\(exit 1\\)",
        ):
            ctx.run(
                ctx.on.action("run-maintenance-script", params={"script": "showJobs"}),
                active_state,
            )
        assert ctx.action_results is None

    def test_mediawiki_status_exception(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mock_mediawiki: MockType,
    ) -> None:
        """Test that MediaWikiStatusException is surfaced in the action failure message."""
        mock_mediawiki.run_maintenance_script.side_effect = MediaWikiBlockedStatusException(
            "MediaWiki is blocked"
        )
        with pytest.raises(
            testing.ActionFailed, match="Maintenance script failed: MediaWiki is blocked"
        ):
            ctx.run(
                ctx.on.action("run-maintenance-script", params={"script": "showJobs"}),
                active_state,
            )
        assert ctx.action_results is None

    def test_unexpected_error(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mock_mediawiki: MockType,
    ) -> None:
        """Test that unexpected errors result in a failure message containing the exception."""
        mock_mediawiki.run_maintenance_script.side_effect = Exception("Unexpected failure")
        with pytest.raises(
            testing.ActionFailed, match="Maintenance script failed: Unexpected failure"
        ):
            ctx.run(
                ctx.on.action("run-maintenance-script", params={"script": "showJobs"}),
                active_state,
            )
        assert ctx.action_results is None


class TestSshKey:
    """Tests for the _ssh_key() helper in charm.py."""

    _FAKE_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----\n"

    def _state_with_ssh_secret(
        self,
        active_state: testing.State,
        content: dict[str, str],
    ) -> tuple[testing.State, str]:
        """Return a state containing a user-owned SSH key secret and the secret's ID.

        User-owned secrets have no app/unit owner in the ops testing model (owner=None).
        """
        secret = testing.Secret(
            content,
        )
        state = dataclasses.replace(
            active_state,
            secrets=[*active_state.secrets, secret],
            config={**active_state.config, "ssh-key": secret.id},
        )
        return state, secret.id

    def test_ssh_key_passed_to_mediawiki_when_configured(
        self, ctx: testing.Context, active_state: testing.State, mock_mediawiki: MockType
    ) -> None:
        """Test that a secret with the 'mediawiki' field passes the key to reconciliation()."""
        state_in, _ = self._state_with_ssh_secret(active_state, {"mediawiki": self._FAKE_KEY})

        ctx.run(ctx.on.config_changed(), state_in)

        mock_mediawiki.reconciliation.assert_called_once()
        _, kwargs = mock_mediawiki.reconciliation.call_args
        assert kwargs.get("ssh_key") == self._FAKE_KEY

    def test_ssh_key_not_passed_when_not_configured(
        self, ctx: testing.Context, active_state: testing.State, mock_mediawiki: MockType
    ) -> None:
        """Test that ssh_key=None is passed when no ssh-key config is set."""
        ctx.run(ctx.on.config_changed(), active_state)

        mock_mediawiki.reconciliation.assert_called_once()
        _, kwargs = mock_mediawiki.reconciliation.call_args
        assert kwargs.get("ssh_key") is None

    def test_ssh_key_none_when_only_git_sync_field(
        self, ctx: testing.Context, active_state: testing.State, mock_mediawiki: MockType
    ) -> None:
        """Test that ssh_key=None when only the git-sync field is present (no mediawiki key)."""
        state_in, _ = self._state_with_ssh_secret(active_state, {"git-sync": self._FAKE_KEY})

        ctx.run(ctx.on.config_changed(), state_in)

        mock_mediawiki.reconciliation.assert_called_once()
        _, kwargs = mock_mediawiki.reconciliation.call_args
        assert kwargs.get("ssh_key") is None

    def test_ssh_key_blocks_on_no_known_fields(
        self, ctx: testing.Context, active_state: testing.State
    ) -> None:
        """Test that a secret with no recognised fields puts the charm in BlockedStatus."""
        state_in, _ = self._state_with_ssh_secret(active_state, {"unknown-field": "value"})

        state_out = ctx.run(ctx.on.config_changed(), state_in)

        assert isinstance(state_out.unit_status, ops.BlockedStatus)
        assert "at least one of" in state_out.unit_status.message

    @pytest.mark.parametrize("blank_value", ["", "   ", "\n", "\t"])
    def test_ssh_key_blocks_on_blank_field_value(
        self, ctx: testing.Context, active_state: testing.State, blank_value: str
    ) -> None:
        """Test that an ssh-key secret with a blank 'mediawiki' value puts the charm in BlockedStatus."""
        state_in, _ = self._state_with_ssh_secret(active_state, {"mediawiki": blank_value})

        state_out = ctx.run(ctx.on.config_changed(), state_in)

        assert isinstance(state_out.unit_status, ops.BlockedStatus)
        assert "must not be empty" in state_out.unit_status.message


class TestPebbleLayer:
    """Tests for the _pebble_layer method and service reconciliation."""

    def test_services_enabled(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mock_mediawiki: MockType,
    ) -> None:
        """Test that all services are running when reconciliation succeeds and redis is ready."""
        mock_mediawiki.runner_queue_service_is_ready.return_value = True

        state_out = ctx.run(ctx.on.config_changed(), active_state)
        assert isinstance(state_out.unit_status, ops.ActiveStatus)

        container = state_out.get_container(Charm._CONTAINER_NAME)
        plan = container.plan
        # Always-on services have startup enabled (managed via replan)
        assert plan.services[Charm._LOGROTATE_SERVICE_NAME].startup == "enabled"
        assert plan.services[Charm._APACHE_EXPORTER_SERVICE_NAME].startup == "enabled"
        assert plan.services[Charm._FRESHCLAM_SERVICE_NAME].startup == "enabled"
        assert plan.services[Charm._CLAMD_SERVICE_NAME].startup == "enabled"
        # Conditional services are explicitly started
        assert container.service_statuses[Charm._SERVICE_NAME] == pebble.ServiceStatus.ACTIVE
        for service in Charm._REDIS_JOB_SERVICES:
            assert container.service_statuses[service] == pebble.ServiceStatus.ACTIVE

    def test_redis_services_stopped_when_not_ready(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mock_mediawiki: MockType,
    ) -> None:
        """Test that redis job services are stopped when runner queue is not ready."""
        mock_mediawiki.runner_queue_service_is_ready.return_value = False

        state_out = ctx.run(ctx.on.config_changed(), active_state)
        assert isinstance(state_out.unit_status, ops.ActiveStatus)

        container = state_out.get_container(Charm._CONTAINER_NAME)
        plan = container.plan
        # Always-on services remain enabled
        assert plan.services[Charm._LOGROTATE_SERVICE_NAME].startup == "enabled"
        assert plan.services[Charm._APACHE_EXPORTER_SERVICE_NAME].startup == "enabled"
        assert plan.services[Charm._FRESHCLAM_SERVICE_NAME].startup == "enabled"
        assert plan.services[Charm._CLAMD_SERVICE_NAME].startup == "enabled"
        # MediaWiki is started
        assert container.service_statuses[Charm._SERVICE_NAME] == pebble.ServiceStatus.ACTIVE
        # Redis job services are stopped
        for service in Charm._REDIS_JOB_SERVICES:
            assert (
                container.service_statuses.get(service, pebble.ServiceStatus.INACTIVE)
                == pebble.ServiceStatus.INACTIVE
            )

    def test_maintenance_stops_workers_before_acknowledgement(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
        mock_mediawiki: MockType,
    ) -> None:
        """A unit acknowledges maintenance only after job workers are stopped."""
        mock_mediawiki.runner_queue_service_is_ready.return_value = True
        relation = dataclasses.replace(
            mediawiki_replica_relation,
            local_app_data={MediaWikiPeers.OPERATION_MODE_KEY: OperationMode.MAINTENANCE.value},
        )
        state_in = dataclasses.replace(
            active_state,
            relations=[
                relation,
                *(item for item in active_state.relations if item.id != relation.id),
            ],
        )

        state_out = ctx.run(
            ctx.on.config_changed(),
            state_in,
        )

        container = state_out.get_container(Charm._CONTAINER_NAME)
        assert container.service_statuses[Charm._SERVICE_NAME] == pebble.ServiceStatus.ACTIVE
        for service in Charm._REDIS_JOB_SERVICES:
            assert (
                container.service_statuses.get(service, pebble.ServiceStatus.INACTIVE)
                == pebble.ServiceStatus.INACTIVE
            )
        out_relation = state_out.get_relation(relation.id)
        assert (
            out_relation.local_unit_data[MediaWikiPeers.OPERATION_MODE_KEY]
            == OperationMode.MAINTENANCE.value
        )
        assert state_out.unit_status == ops.ActiveStatus("MediaWiki maintenance mode enabled")

    def test_workers_restart_before_maintenance_acknowledgement_is_cleared(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
        mock_mediawiki: MockType,
    ) -> None:
        """Leaving maintenance starts workers before publishing read-write state."""
        mock_mediawiki.runner_queue_service_is_ready.return_value = True
        relation = dataclasses.replace(
            mediawiki_replica_relation,
            local_app_data={MediaWikiPeers.OPERATION_MODE_KEY: OperationMode.MAINTENANCE.value},
        )
        state_in = dataclasses.replace(
            active_state,
            relations=[
                relation,
                *(item for item in active_state.relations if item.id != relation.id),
            ],
        )
        maintenance_state = ctx.run(
            ctx.on.config_changed(),
            state_in,
        )
        maintenance_relation = maintenance_state.get_relation(relation.id)
        disabled_relation = dataclasses.replace(
            maintenance_relation,
            local_app_data={MediaWikiPeers.OPERATION_MODE_KEY: OperationMode.NORMAL.value},
        )
        state_in = dataclasses.replace(
            maintenance_state,
            relations=[
                disabled_relation,
                *(item for item in maintenance_state.relations if item.id != disabled_relation.id),
            ],
        )

        state_out = ctx.run(
            ctx.on.config_changed(),
            state_in,
        )

        container = state_out.get_container(Charm._CONTAINER_NAME)
        for service in Charm._REDIS_JOB_SERVICES:
            assert container.service_statuses[service] == pebble.ServiceStatus.ACTIVE
        out_relation = state_out.get_relation(disabled_relation.id)
        assert (
            out_relation.local_unit_data[MediaWikiPeers.OPERATION_MODE_KEY]
            == OperationMode.NORMAL.value
        )
        assert isinstance(state_out.unit_status, ops.ActiveStatus)

    def test_services_stopped_on_pre_reconciliation_failure(
        self,
        ctx: testing.Context,
        base_state: testing.State,
        mediawiki_container: testing.Container,
        mock_mediawiki: MockType,
    ) -> None:
        """Test that mediawiki and redis services are stopped when pre-reconciliation fails (no database)."""
        mock_mediawiki.runner_queue_service_is_ready.return_value = True

        # No database relation means _reconcile_services(enabled=False) is called
        state_out = ctx.run(ctx.on.pebble_ready(container=mediawiki_container), base_state)
        assert isinstance(state_out.unit_status, ops.BlockedStatus)

        container = state_out.get_container(Charm._CONTAINER_NAME)
        plan = container.plan
        # Always-on services remain enabled
        assert plan.services[Charm._LOGROTATE_SERVICE_NAME].startup == "enabled"
        assert plan.services[Charm._APACHE_EXPORTER_SERVICE_NAME].startup == "enabled"
        assert plan.services[Charm._FRESHCLAM_SERVICE_NAME].startup == "enabled"
        assert plan.services[Charm._CLAMD_SERVICE_NAME].startup == "enabled"
        # Conditional services are stopped
        assert (
            container.service_statuses.get(Charm._SERVICE_NAME, pebble.ServiceStatus.INACTIVE)
            == pebble.ServiceStatus.INACTIVE
        )
        for service in Charm._REDIS_JOB_SERVICES:
            assert (
                container.service_statuses.get(service, pebble.ServiceStatus.INACTIVE)
                == pebble.ServiceStatus.INACTIVE
            )


class TestComposerLockPeerSync:
    """Tests for the charm-level composer lock coordination between leader and non-leader units.

    Note: The actual leader-publishes/non-leader-consumes composer lock logic lives entirely
    inside `MediaWiki._reconcile_configuration`, and is exercised with the real (non-mocked)
    workload in `tests/unit/test_mediawiki.py::TestComposerLockSync`. The tests here only cover
    the charm's boundary behaviour: how `MediaWikiWaitingStatusException` from reconciliation
    surfaces as unit status, and that the charm does not itself pass Composer state as kwargs.
    """

    def test_non_leader_waits_when_lock_not_in_peer_data(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mock_mediawiki: MockType,
    ) -> None:
        """Non-leader: WaitingStatus when mediawiki signals the lock is not yet available."""
        # mediawiki.reconciliation() raises WaitingStatus when non-leader has no lock.
        mock_mediawiki.reconciliation.side_effect = MediaWikiWaitingStatusException(
            "Waiting for leader to publish composer lock"
        )
        state_in = dataclasses.replace(active_state, leader=False)
        state_out = ctx.run(ctx.on.config_changed(), state_in)

        assert isinstance(state_out.unit_status, ops.WaitingStatus)
        assert "composer lock" in state_out.unit_status.message.lower()

    def test_non_leader_passes_lock_to_mediawiki_when_available(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
        mock_mediawiki: MockType,
    ) -> None:
        """Charm does not pass peer Composer state into MediaWiki reconciliation."""
        with ctx(ctx.on.update_status(), active_state) as manager:
            state_hash = manager.charm.load_charm_config().state_hash
        lock_rel = dataclasses.replace(
            mediawiki_replica_relation,
            local_app_data={
                Charm._COMPOSER_LOCK_KEY: MOCK_COMPOSER_LOCK,
                Charm._COMPOSER_CONFIG_HASH_KEY: state_hash,
            },
        )
        # Replace the existing peer relation in active_state with one containing lock data.
        relations_without_peer = [
            r for r in active_state.relations if r.endpoint != Charm._PEER_RELATION_NAME
        ]
        state_in = dataclasses.replace(
            active_state,
            leader=False,
            relations=[*relations_without_peer, lock_rel],
        )
        ctx.run(ctx.on.config_changed(), state_in)

        mock_mediawiki.reconciliation.assert_called_once()
        _, kwargs = mock_mediawiki.reconciliation.call_args
        assert "composer_lock" not in kwargs
        assert "composer_config_hash" not in kwargs


class TestMetricsEndpoint:
    """Tests that scrape jobs reflect the desired workload state."""

    @pytest.fixture
    def metrics_relation(self) -> testing.Relation:
        """Return a metrics-endpoint relation."""
        return testing.Relation(endpoint="metrics-endpoint", interface="prometheus_scrape")

    @staticmethod
    def _published_jobs(state_out: testing.State, relation_id: int) -> list[dict[str, Any]]:
        """Return the scrape jobs published in the metrics-endpoint relation data."""
        data = state_out.get_relation(relation_id).local_app_data.get("scrape_jobs")
        if not data:
            return []
        return json.loads(data)

    @staticmethod
    def _published_job_names(state_out: testing.State, relation_id: int) -> list[str]:
        """Return the job names published in the metrics-endpoint relation data."""
        return [
            job.get("job_name", "")
            for job in TestMetricsEndpoint._published_jobs(state_out, relation_id)
        ]

    def test_php_fpm_job_published_as_openmetrics(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        metrics_relation: testing.Relation,
    ) -> None:
        """Publish PHP-FPM's OpenMetrics endpoint through the metrics relation."""
        state_in = dataclasses.replace(
            active_state, relations=active_state.relations | {metrics_relation}
        )

        state_out = ctx.run(ctx.on.config_changed(), state_in)

        jobs = self._published_jobs(state_out, metrics_relation.id)
        php_fpm_job = next(job for job in jobs if job["job_name"] == "mediawiki_php_fpm")
        assert php_fpm_job["metrics_path"] == "/php-fpm-status"
        assert php_fpm_job["params"] == {"openmetrics": [""]}
        assert php_fpm_job["static_configs"] == [{"targets": ["*:8091"]}]

    def test_apache_job_always_published_when_active(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        metrics_relation: testing.Relation,
        mock_git_sync: MockType,
    ) -> None:
        """Apache exporter job is advertised (always-on service) and git-sync is not when unconfigured."""
        mock_git_sync.metrics_scrape_jobs.return_value = []
        state_in = dataclasses.replace(
            active_state, relations=[*active_state.relations, metrics_relation]
        )

        state_out = ctx.run(ctx.on.config_changed(), state_in)

        job_names = self._published_job_names(state_out, metrics_relation.id)
        assert any("apache_exporter" in name for name in job_names)
        assert not any("git_sync" in name for name in job_names)

    @pytest.mark.parametrize(
        ("job_name", "target"),
        [
            ("shellbox_apache_exporter", "*:9118"),
            ("shellbox_php_fpm", "*:9253"),
        ],
    )
    def test_shellbox_jobs_publish_metrics_targets(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        metrics_relation: testing.Relation,
        job_name: str,
        target: str,
    ) -> None:
        """Publish each Shellbox metrics endpoint with its expected scrape target."""
        state_in = dataclasses.replace(
            active_state, relations=[*active_state.relations, metrics_relation]
        )

        state_out = ctx.run(ctx.on.config_changed(), state_in)

        jobs = json.loads(
            state_out.get_relation(metrics_relation.id).local_app_data["scrape_jobs"]
        )
        matching_jobs = [job for job in jobs if job["job_name"] == job_name]
        assert len(matching_jobs) == 1
        assert matching_jobs[0]["static_configs"] == [{"targets": [target]}]

    def test_git_sync_job_published_when_metrics_enabled(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        metrics_relation: testing.Relation,
        mock_git_sync: MockType,
    ) -> None:
        """Git-sync job is advertised when git-sync's lookaside callable contributes it."""
        mock_git_sync.metrics_scrape_jobs.return_value = [
            {"job_name": "git_sync", "static_configs": [{"targets": ["*:8081"]}]}
        ]
        state_in = dataclasses.replace(
            active_state, relations=[*active_state.relations, metrics_relation]
        )

        state_out = ctx.run(ctx.on.config_changed(), state_in)

        job_names = self._published_job_names(state_out, metrics_relation.id)
        assert any("apache_exporter" in name for name in job_names)
        assert any("git_sync" in name for name in job_names)

    def test_git_sync_job_retained_when_blocked(
        self,
        ctx: testing.Context,
        base_state: testing.State,
        metrics_relation: testing.Relation,
        mediawiki_container: testing.Container,
        mock_git_sync: MockType,
    ) -> None:
        """Git-sync job is published when the charm blocks before reconciliation completes.

        The provider re-publishes its jobs on refresh events (including pebble-ready)
        and the git-sync job is contributed by the lookaside callable, so it does not
        depend on reconciliation running to completion.
        """
        mock_git_sync.metrics_scrape_jobs.return_value = [
            {"job_name": "git_sync", "static_configs": [{"targets": ["*:8081"]}]}
        ]
        state_in = dataclasses.replace(base_state, relations=[metrics_relation])

        state_out = ctx.run(ctx.on.pebble_ready(container=mediawiki_container), state_in)

        assert isinstance(state_out.unit_status, ops.BlockedStatus)
        job_names = self._published_job_names(state_out, metrics_relation.id)
        # apache-exporter is always-on so its job is still advertised once pebble is ready.
        assert any("apache_exporter" in name for name in job_names)
        # git-sync's lookaside contributes its job regardless of the charm status.
        assert any("git_sync" in name for name in job_names)

    def test_git_sync_job_omitted_when_lookaside_empty(
        self,
        ctx: testing.Context,
        base_state: testing.State,
        metrics_relation: testing.Relation,
        mediawiki_container: testing.Container,
    ) -> None:
        """Git-sync job is omitted when its lookaside callable contributes nothing."""
        disconnected = dataclasses.replace(mediawiki_container, can_connect=False)
        other = [c for c in base_state.containers if c.name != Charm._CONTAINER_NAME]
        state_in = dataclasses.replace(
            base_state, containers=[*other, disconnected], relations=[metrics_relation]
        )

        state_out = ctx.run(ctx.on.config_changed(), state_in)

        assert isinstance(state_out.unit_status, ops.WaitingStatus)
        # The lookaside (default mock) returns no jobs, so git-sync is not advertised.
        job_names = self._published_job_names(state_out, metrics_relation.id)
        assert not any("git_sync" in name for name in job_names)
