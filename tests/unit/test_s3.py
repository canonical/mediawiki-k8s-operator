# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import pytest
from ops import CharmBase, testing
from pytest_mock import MockerFixture, MockType

import s3
from exceptions import MediaWikiBlockedStatusException
from types_ import S3ConnectionInfo


class WrapperCharm(CharmBase):
    """A minimal wrapper charm class to build a testing context with."""

    def __init__(self, *args):
        super().__init__(*args)
        self.s3 = s3.S3(self, "s3-parameters")


@pytest.fixture
def mock_s3_requirer(mocker: MockerFixture) -> MockType:
    """Fixture to mock the S3Requirer class from charms.data_platform_libs.v0.s3."""
    mock_s3_requirer_cls = mocker.patch("s3.S3Requirer")
    mock_instance = mock_s3_requirer_cls.return_value
    mock_instance.relation_name = "s3-parameters"
    mock_instance.relations = []

    return mock_instance


@pytest.fixture
def ctx(meta: dict) -> testing.Context:
    """Provide a Context with the charm root set."""
    meta = meta.copy()
    config = meta.pop("config", None)
    actions = meta.pop("actions", None)
    return testing.Context(WrapperCharm, meta=meta, config=config, actions=actions)


@pytest.fixture
def s3_relation() -> testing.Relation:
    """Provide an S3 relation."""
    return testing.Relation(
        "s3-parameters",
        id=0,
        remote_app_name="s3",
    )


@pytest.fixture
def active_state_with_s3(
    s3_relation: testing.Relation,
) -> testing.State:
    """Provide a state with S3 relation."""
    return testing.State(
        relations=[s3_relation],
        leader=True,
    )


class TestHasRelation:
    """Tests for the S3.has_relation method."""

    def test_has_relation_when_relation_exists(
        self,
        ctx: testing.Context,
        active_state_with_s3: testing.State,
        mock_s3_requirer: MockType,
    ):
        """Test that has_relation returns True when S3 relation exists."""
        mock_s3_requirer.relations = [testing.Relation("s3-parameters")]

        with ctx(ctx.on.update_status(), active_state_with_s3) as mgr:
            result = mgr.charm.s3.has_relation()
            assert result is True

    def test_has_relation_when_relation_does_not_exist(
        self,
        ctx: testing.Context,
        base_state: testing.State,
        mock_s3_requirer: MockType,
    ):
        """Test that has_relation returns False when S3 relation does not exist."""
        mock_s3_requirer.relations = []

        with ctx(ctx.on.update_status(), base_state) as mgr:
            result = mgr.charm.s3.has_relation()
            assert result is False

    def test_has_relation_when_relations_list_empty(
        self,
        ctx: testing.Context,
        active_state_with_s3: testing.State,
        mock_s3_requirer: MockType,
    ):
        """Test that has_relation returns False when relations list is empty even if model has relation."""
        mock_s3_requirer.relations = []

        with ctx(ctx.on.update_status(), active_state_with_s3) as mgr:
            assert mgr.charm.s3.has_relation() is False, (
                "has_relation should return False when relations list is empty"
            )


class TestGetRelationData:
    """Tests for the S3.get_relation_data method."""

    @pytest.fixture
    def valid_s3_data(self) -> dict:
        """Provide valid S3 connection data."""
        return {
            "endpoint": "https://s3.example.com",
            "bucket": "my-bucket",
            "access-key": "AKIAIOSFODNN7EXAMPLE",
            "secret-key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "region": "us-east-1",
            "s3-uri-style": "virtual",
        }

    def test_valid_relation_data(
        self,
        ctx: testing.Context,
        active_state_with_s3: testing.State,
        mock_s3_requirer: MockType,
        valid_s3_data: dict,
    ):
        """Test that valid S3 relation data is returned correctly."""
        mock_s3_requirer.relations = [testing.Relation("s3-parameters")]
        mock_s3_requirer.get_s3_connection_info.return_value = valid_s3_data.copy()

        with ctx(ctx.on.update_status(), active_state_with_s3) as mgr:
            data = mgr.charm.s3.get_relation_data()

            assert isinstance(data, S3ConnectionInfo)
            assert data.endpoint == valid_s3_data["endpoint"]
            assert data.bucket == valid_s3_data["bucket"]
            assert data.access_key == valid_s3_data["access-key"]
            assert data.secret_key == valid_s3_data["secret-key"]  # nosec: B105
            assert data.region == valid_s3_data["region"]
            assert data.s3_uri_style == valid_s3_data["s3-uri-style"]

    def test_valid_relation_data_with_tls_ca_chain(
        self,
        ctx: testing.Context,
        active_state_with_s3: testing.State,
        mock_s3_requirer: MockType,
        valid_s3_data: dict,
    ):
        """Test that S3 relation data with TLS CA chain is parsed correctly."""
        valid_s3_data["tls-ca-chain"] = [
            "-----BEGIN CERTIFICATE-----\ncert1\n-----END CERTIFICATE-----",
            "-----BEGIN CERTIFICATE-----\ncert2\n-----END CERTIFICATE-----",
        ]
        mock_s3_requirer.relations = [testing.Relation("s3-parameters")]
        mock_s3_requirer.get_s3_connection_info.return_value = valid_s3_data.copy()

        with ctx(ctx.on.update_status(), active_state_with_s3) as mgr:
            data = mgr.charm.s3.get_relation_data()

            assert data.tls_ca_chain == valid_s3_data["tls-ca-chain"]
            assert "\n\n" in data.ca_cert

    def test_valid_relation_data_minimal(
        self,
        ctx: testing.Context,
        active_state_with_s3: testing.State,
        mock_s3_requirer: MockType,
    ):
        """Test that minimal required S3 relation data is parsed correctly."""
        minimal_data = {
            "endpoint": "https://s3.example.com",
            "bucket": "my-bucket",
            "access-key": "AKIAIOSFODNN7EXAMPLE",
            "secret-key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        }
        mock_s3_requirer.relations = [testing.Relation("s3-parameters")]
        mock_s3_requirer.get_s3_connection_info.return_value = minimal_data.copy()

        with ctx(ctx.on.update_status(), active_state_with_s3) as mgr:
            data = mgr.charm.s3.get_relation_data()

            assert data.endpoint == minimal_data["endpoint"]
            assert data.bucket == minimal_data["bucket"]
            assert data.access_key == minimal_data["access-key"]
            assert data.secret_key == minimal_data["secret-key"]  # nosec: B105
            assert data.region is None
            assert data.s3_uri_style is None

    def test_relation_not_present(
        self,
        ctx: testing.Context,
        base_state: testing.State,
        mock_s3_requirer: MockType,
    ):
        """Test that MediaWikiBlockedStatusException is raised when S3 relation does not exist."""
        mock_s3_requirer.relation_name = "s3-parameters"
        mock_s3_requirer.relations = []

        with (
            ctx(ctx.on.update_status(), base_state) as mgr,
            pytest.raises(
                MediaWikiBlockedStatusException,
                match="Waiting for relation s3-parameters",
            ),
        ):
            mgr.charm.s3.get_relation_data()

    def test_missing_endpoint(
        self,
        ctx: testing.Context,
        active_state_with_s3: testing.State,
        mock_s3_requirer: MockType,
        valid_s3_data: dict,
    ):
        """Test that ValidationError is caught when endpoint is missing."""
        del valid_s3_data["endpoint"]
        mock_s3_requirer.relations = [testing.Relation("s3-parameters")]
        mock_s3_requirer.get_s3_connection_info.return_value = valid_s3_data
        mock_s3_requirer.relation_name = "s3-parameters"

        with (
            ctx(ctx.on.update_status(), active_state_with_s3) as mgr,
            pytest.raises(
                MediaWikiBlockedStatusException,
                match="Error fetching s3-parameters relation data",
            ),
        ):
            mgr.charm.s3.get_relation_data()

    def test_missing_bucket(
        self,
        ctx: testing.Context,
        active_state_with_s3: testing.State,
        mock_s3_requirer: MockType,
        valid_s3_data: dict,
    ):
        """Test that ValidationError is caught when bucket is missing."""
        del valid_s3_data["bucket"]
        mock_s3_requirer.relations = [testing.Relation("s3-parameters")]
        mock_s3_requirer.get_s3_connection_info.return_value = valid_s3_data
        mock_s3_requirer.relation_name = "s3-parameters"

        with (
            ctx(ctx.on.update_status(), active_state_with_s3) as mgr,
            pytest.raises(
                MediaWikiBlockedStatusException,
                match="Error fetching s3-parameters relation data",
            ),
        ):
            mgr.charm.s3.get_relation_data()

    def test_missing_access_key(
        self,
        ctx: testing.Context,
        active_state_with_s3: testing.State,
        mock_s3_requirer: MockType,
        valid_s3_data: dict,
    ):
        """Test that ValidationError is caught when access-key is missing."""
        del valid_s3_data["access-key"]
        mock_s3_requirer.relations = [testing.Relation("s3-parameters")]
        mock_s3_requirer.get_s3_connection_info.return_value = valid_s3_data
        mock_s3_requirer.relation_name = "s3-parameters"

        with (
            ctx(ctx.on.update_status(), active_state_with_s3) as mgr,
            pytest.raises(
                MediaWikiBlockedStatusException,
                match="Error fetching s3-parameters relation data",
            ),
        ):
            mgr.charm.s3.get_relation_data()

    def test_missing_secret_key(
        self,
        ctx: testing.Context,
        active_state_with_s3: testing.State,
        mock_s3_requirer: MockType,
        valid_s3_data: dict,
    ):
        """Test that ValidationError is caught when secret-key is missing."""
        del valid_s3_data["secret-key"]
        mock_s3_requirer.relations = [testing.Relation("s3-parameters")]
        mock_s3_requirer.get_s3_connection_info.return_value = valid_s3_data
        mock_s3_requirer.relation_name = "s3-parameters"

        with (
            ctx(ctx.on.update_status(), active_state_with_s3) as mgr,
            pytest.raises(
                MediaWikiBlockedStatusException,
                match="Error fetching s3-parameters relation data",
            ),
        ):
            mgr.charm.s3.get_relation_data()

    def test_malformed_data_type(
        self,
        ctx: testing.Context,
        active_state_with_s3: testing.State,
        mock_s3_requirer: MockType,
        valid_s3_data: dict,
    ):
        """Test that ValidationError is caught when data types are invalid."""
        valid_s3_data["endpoint"] = 12345  # Should be string
        mock_s3_requirer.relations = [testing.Relation("s3-parameters")]
        mock_s3_requirer.get_s3_connection_info.return_value = valid_s3_data
        mock_s3_requirer.relation_name = "s3-parameters"

        with (
            ctx(ctx.on.update_status(), active_state_with_s3) as mgr,
            pytest.raises(
                MediaWikiBlockedStatusException,
                match="Error fetching s3-parameters relation data",
            ),
        ):
            mgr.charm.s3.get_relation_data()
