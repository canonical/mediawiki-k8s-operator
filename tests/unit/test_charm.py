# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import copy
import dataclasses
import json

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
from tests.unit.conftest import MOCK_COMPOSER_LOCK


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
        workload.update_database_schema = mock_instance.update_database_schema
        workload.runner_queue_service_is_ready = mock_instance.runner_queue_service_is_ready
        return workload.reconciliation(*args, **kwargs)

    # Setup default return values or side effects
    mock_instance.reconciliation.side_effect = reconcile_with_workload
    mock_instance.create_and_promote_user.return_value = "mocked-password"  # nosec: B105
    mock_instance.update_database_schema.return_value = None
    mock_instance._reconcile_configuration.return_value = None
    mock_instance.runner_queue_service_is_ready.return_value = False

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


class TestGeneralEvents:
    def test_invalid_proxy_config(
        self, ctx: testing.Context, active_state: testing.State, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that we block should invalid proxy configurations be provided"""
        monkeypatch.setenv("JUJU_CHARM_HTTP_PROXY", "invalid")
        state_out = ctx.run(ctx.on.update_status(), active_state)
        assert isinstance(state_out.unit_status, ops.BlockedStatus)


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
        secrets: list[testing.Secret],
    ) -> None:
        """Test that the charm sets waiting status when pebble cannot connect."""
        mediawiki_container = dataclasses.replace(mediawiki_container, can_connect=False)

        # With replica secrets
        state_in = testing.State(
            containers=[mediawiki_container],
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
        mediawiki_replica_relation: testing.PeerRelation,
        active_state: testing.State,
        app_ro: str,
        peers_ro: dict[int, str | None],
        *,
        unit_ro: str | None = None,
        leader: bool = True,
    ) -> tuple[testing.PeerRelation, testing.State]:
        """Helper method to configure the replica relation RO flags and build the input state.

        Args:
            mediawiki_replica_relation: The base peer relation fixture to configure.
            active_state: The base state to extend with the configured relation.
            app_ro: The app-level RO database flag value ("true" or "false").
            peers_ro: Mapping of peer unit ID to its RO flag value, or None if the flag should be absent.
            unit_ro: The local unit RO flag value, or None if the flag should be absent.
            leader: Whether the local unit should be the leader.

        Returns:
            The configured (replica_relation, state_in) tuple.
        """
        peers_data = {
            unit_id: ({Charm._RO_DATABASE_FLAG: ro} if ro is not None else {})
            for unit_id, ro in peers_ro.items()
        }
        replace_kwargs: dict = {
            "local_app_data": {
                Charm._RO_DATABASE_FLAG: app_ro,
                Charm._COMPOSER_JSON_KEY: "{}",
                Charm._COMPOSER_LOCK_KEY: MOCK_COMPOSER_LOCK,
            },
            "peers_data": peers_data,
        }
        if unit_ro is not None:
            replace_kwargs["local_unit_data"] = {Charm._RO_DATABASE_FLAG: unit_ro}
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
            mediawiki_replica_relation,
            active_state,
            app_ro="true",
            peers_ro={1: None, 2: None},
            leader=False,
        )
        state_out = ctx.run(
            ctx.on.relation_changed(relation=mediawiki_replica_relation, remote_unit=0), state_in
        )
        assert isinstance(state_out.unit_status, ops.MaintenanceStatus)

        out_replica_relation = state_out.get_relation(mediawiki_replica_relation.id)
        assert out_replica_relation.local_app_data[Charm._RO_DATABASE_FLAG] == "true"
        assert out_replica_relation.local_unit_data[Charm._RO_DATABASE_FLAG] == "true"

    def test_leader_waits_for_replicas(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
    ) -> None:
        """Test that the leader unit waits for all replicas to indicate that they are in a read only mode before proceeding with a database update."""
        mediawiki_replica_relation, state_in = self._setup(
            mediawiki_replica_relation,
            active_state,
            app_ro="true",
            peers_ro={1: "true", 2: None},
        )
        state_out = ctx.run(
            ctx.on.relation_changed(relation=mediawiki_replica_relation, remote_unit=0), state_in
        )
        assert isinstance(state_out.unit_status, ops.WaitingStatus)

        out_replica_relation = state_out.get_relation(mediawiki_replica_relation.id)
        assert out_replica_relation.local_app_data[Charm._RO_DATABASE_FLAG] == "true"
        assert out_replica_relation.local_unit_data[Charm._RO_DATABASE_FLAG] == "true"

    def test_database_update(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
    ) -> None:
        """Test that the motions of a database update occurs once all units indicate a read only mode, and the application level read only database flag is set."""
        mediawiki_replica_relation, state_in = self._setup(
            mediawiki_replica_relation,
            active_state,
            app_ro="true",
            peers_ro={1: "true", 2: "true"},
        )
        state_out = ctx.run(
            ctx.on.relation_changed(relation=mediawiki_replica_relation, remote_unit=0), state_in
        )
        assert isinstance(state_out.unit_status, ops.MaintenanceStatus)

        out_replica_relation = state_out.get_relation(mediawiki_replica_relation.id)
        assert out_replica_relation.local_app_data[Charm._RO_DATABASE_FLAG] == "false"
        assert out_replica_relation.local_unit_data[Charm._RO_DATABASE_FLAG] == "true"

    def test_only_leader_performs_update(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
    ) -> None:
        """Test that only the leader unit performs the database update when the read only database flag is set, even if all units indicate they are in a read only mode."""
        mediawiki_replica_relation, state_in = self._setup(
            mediawiki_replica_relation,
            active_state,
            app_ro="true",
            peers_ro={1: "true", 2: "true"},
            leader=False,
        )
        state_out = ctx.run(
            ctx.on.relation_changed(relation=mediawiki_replica_relation, remote_unit=0), state_in
        )
        assert isinstance(state_out.unit_status, ops.MaintenanceStatus)

        out_replica_relation = state_out.get_relation(mediawiki_replica_relation.id)
        assert out_replica_relation.local_app_data[Charm._RO_DATABASE_FLAG] == "true"
        assert out_replica_relation.local_unit_data[Charm._RO_DATABASE_FLAG] == "true"

    def test_update_skipped_when_flag_not_set(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
        mock_mediawiki: MockType,
    ) -> None:
        """Test that the database update is skipped if all units indicate they are in a read only mode, but the application level read only database flag is not set."""
        mediawiki_replica_relation, state_in = self._setup(
            mediawiki_replica_relation,
            active_state,
            app_ro="false",
            peers_ro={1: "true", 2: "false"},
            unit_ro="true",
        )

        mock_mediawiki.update_database_schema.side_effect = MediaWikiInstallError(
            "Mocked install error during database update"
        )

        state_out = ctx.run(
            ctx.on.relation_changed(relation=mediawiki_replica_relation, remote_unit=0), state_in
        )
        assert isinstance(state_out.unit_status, ops.ActiveStatus)

        out_replica_relation = state_out.get_relation(mediawiki_replica_relation.id)
        assert out_replica_relation.local_app_data[Charm._RO_DATABASE_FLAG] == "false"
        assert out_replica_relation.local_unit_data[Charm._RO_DATABASE_FLAG] == "false"

    def test_database_update_failure(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
        mock_mediawiki: MockType,
    ) -> None:
        """Test that should a database update fail, the charm enters an error state and a MediaWikiInstallError exception is raised."""
        mediawiki_replica_relation, state_in = self._setup(
            mediawiki_replica_relation,
            active_state,
            app_ro="true",
            peers_ro={1: "true", 2: "true"},
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


class TestRotateMediaWikiSecretsAction:
    def test_success(
        self, ctx: testing.Context, active_state: testing.State, mocker: MockerFixture
    ) -> None:
        """Test that rotate-mediawiki-secrets updates replica secret content."""
        expected_secret_content = {
            "key": "new-mocked-key",
            "session": "new-mocked-session",
            "saml-salt": "new-mocked-saml-salt",
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
        assert call_kwargs.get("force_composer_update") is True

    def test_non_leader_runs_forced_reconciliation(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
        mock_mediawiki: MockType,
    ) -> None:
        """Test that a non-leader can run a forced reconciliation on itself."""
        # Non-leaders require lock data in peer relation to proceed past the composer gate.
        lock_rel = dataclasses.replace(
            mediawiki_replica_relation,
            local_app_data={
                Charm._COMPOSER_JSON_KEY: "{}",
                Charm._COMPOSER_LOCK_KEY: MOCK_COMPOSER_LOCK,
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
        assert call_kwargs.get("force_composer_update") is True

    def test_all_units_sets_app_flag(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
        mock_mediawiki: MockType,
    ) -> None:
        """Test that all-units=true sets the app-level flag without running reconciliation."""
        state_out = ctx.run(
            ctx.on.action("force-reconciliation", params={"all-units": True}), active_state
        )
        replica_relation = state_out.get_relation(mediawiki_replica_relation.id)
        assert replica_relation.local_app_data[Charm._FORCE_RECONCILIATION_FLAG] == "true"
        mock_mediawiki.reconciliation.assert_not_called()

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
    """Tests for the charm-level composer lock coordination between leader and non-leader units."""

    def test_leader_publishes_lock_to_peer_relation(
        self,
        ctx: testing.Context,
        configured_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
        mock_mediawiki: MockType,
    ) -> None:
        """Leader Composer state is published within MediaWiki reconciliation."""
        mock_mediawiki._reconcile_configuration.return_value = MOCK_COMPOSER_LOCK

        # configured_state already contains mediawiki_replica_relation; use it directly.
        state_out = ctx.run(ctx.on.config_changed(), configured_state)

        replica_rel = state_out.get_relation(mediawiki_replica_relation.id)
        assert replica_rel.local_app_data.get(Charm._COMPOSER_LOCK_KEY) == MOCK_COMPOSER_LOCK
        assert replica_rel.local_app_data.get(Charm._COMPOSER_JSON_KEY) is not None

    def test_leader_does_not_publish_when_no_lock_returned(
        self,
        ctx: testing.Context,
        active_state: testing.State,
        mediawiki_replica_relation: testing.PeerRelation,
        mock_mediawiki: MockType,
    ) -> None:
        """Leader leaves peer data unchanged when configuration returns no lock."""
        mock_mediawiki._reconcile_configuration.return_value = None

        # active_state already contains mediawiki_replica_relation; use it directly.
        state_out = ctx.run(ctx.on.config_changed(), active_state)

        replica_rel = state_out.get_relation(mediawiki_replica_relation.id)
        assert Charm._COMPOSER_LOCK_KEY not in replica_rel.local_app_data
        assert Charm._COMPOSER_JSON_KEY not in replica_rel.local_app_data

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
        lock_rel = dataclasses.replace(
            mediawiki_replica_relation,
            local_app_data={
                Charm._COMPOSER_JSON_KEY: json.dumps({"require": {}}),
                Charm._COMPOSER_LOCK_KEY: MOCK_COMPOSER_LOCK,
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
        assert "peer_composer_json" not in kwargs


class TestMetricsEndpoint:
    """Tests that scrape jobs reflect the desired workload state."""

    @pytest.fixture
    def metrics_relation(self) -> testing.Relation:
        """Return a metrics-endpoint relation."""
        return testing.Relation(endpoint="metrics-endpoint", interface="prometheus_scrape")

    @staticmethod
    def _published_job_names(state_out: testing.State, relation_id: int) -> list[str]:
        """Return the job names published in the metrics-endpoint relation data."""
        data = state_out.get_relation(relation_id).local_app_data.get("scrape_jobs")
        if not data:
            return []
        return [job.get("job_name", "") for job in json.loads(data)]

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
