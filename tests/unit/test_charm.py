# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import copy
import dataclasses

import ops
import pytest
from ops import testing
from pytest_mock import MockerFixture, MockType

from charm import Charm
from exceptions import MediaWikiBlockedStatusException, MediaWikiInstallError
from mediawiki_api import SiteInfo


@pytest.fixture(autouse=True)
def mock_mediawiki(mocker: MockerFixture) -> MockType:
    """Base MediaWiki class mock."""
    # We patch where it is imported in the charm.py file, not where it is defined
    mock_mediawiki_cls = mocker.patch("charm.MediaWiki", autospec=True)
    mock_instance = mock_mediawiki_cls.return_value

    # Setup default return values or side effects
    mock_instance.reconciliation.return_value = None
    mock_instance.rotate_root_credentials.return_value = ("mocked-root", "mocked-password")
    mock_instance.update_database_schema.return_value = None

    return mock_instance


@pytest.fixture(autouse=True)
def mock_site_info(mocker: MockerFixture) -> SiteInfo:
    """Mock SiteInfo.fetch to return a SiteInfo with default test data."""
    info = SiteInfo(
        {
            "general": {
                "generator": "MediaWiki 1.45.0",
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
            unit_id: testing.RawDataBagContents(
                {Charm._RO_DATABASE_FLAG: ro} if ro is not None else {}
            )
            for unit_id, ro in peers_ro.items()
        }
        replace_kwargs: dict = {
            "local_app_data": testing.RawDataBagContents({Charm._RO_DATABASE_FLAG: app_ro}),
            "peers_data": peers_data,
        }
        if unit_ro is not None:
            replace_kwargs["local_unit_data"] = testing.RawDataBagContents(
                {Charm._RO_DATABASE_FLAG: unit_ro}
            )
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


class TestRotateRootCredentialsAction:
    def test_success(self, ctx: testing.Context, active_state: testing.State) -> None:
        """Test that the action returns new credentials when rotation is successful."""
        ctx.run(ctx.on.action("rotate-root-credentials"), active_state)

        # no sec B105 is bugged for multi-line dicts https://github.com/PyCQA/bandit/issues/1352
        mocked_password = "mocked-password"  # nosec: B105
        assert ctx.action_results == {"username": "mocked-root", "password": mocked_password}

    def test_failure(
        self, ctx: testing.Context, active_state: testing.State, mock_mediawiki
    ) -> None:
        """Test that the action fails when MediaWiki raises an exception during credential rotation."""
        mock_mediawiki.rotate_root_credentials.side_effect = MediaWikiBlockedStatusException(
            "Mocked blocked status during credential rotation"
        )

        with pytest.raises(
            testing.ActionFailed, match="Mocked blocked status during credential rotation"
        ):
            ctx.run(ctx.on.action("rotate-root-credentials"), active_state)

        assert ctx.action_results is None

        mock_mediawiki.rotate_root_credentials.side_effect = Exception(
            "Mocked exception during credential rotation"
        )

        with pytest.raises(testing.ActionFailed, match="Credential rotation failed"):
            ctx.run(ctx.on.action("rotate-root-credentials"), active_state)

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
            testing.RawSecretRevisionContents(content),
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
