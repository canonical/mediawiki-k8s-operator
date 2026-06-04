# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import pytest
from charms.smtp_integrator.v0.smtp import AuthType, SmtpRelationData, TransportSecurity
from ops import CharmBase, testing
from pytest_mock import MockerFixture, MockType

import smtp
from exceptions import MediaWikiBlockedStatusException, MediaWikiWaitingStatusException


class WrapperCharm(CharmBase):
    """A minimal wrapper charm class to build a testing context with."""

    def __init__(self, *args):
        super().__init__(*args)
        self.smtp = smtp.Smtp(self, "smtp")


@pytest.fixture()
def mock_smtp_requires(mocker: MockerFixture) -> MockType:
    """Fixture to mock the SmtpRequires class."""
    mock_cls = mocker.patch("smtp.SmtpRequires")
    mock_instance = mock_cls.return_value
    mock_instance.relation_name = "smtp"
    return mock_instance


@pytest.fixture()
def ctx(meta: dict) -> testing.Context:
    """Provide a Context with the charm root set."""
    meta = meta.copy()
    config = meta.pop("config", None)
    actions = meta.pop("actions", None)
    return testing.Context(WrapperCharm, meta=meta, config=config, actions=actions)


@pytest.fixture()
def smtp_relation() -> testing.Relation:
    """Provide an SMTP relation."""
    return testing.Relation(
        "smtp",
        id=0,
        remote_app_name="smtp-integrator",
    )


@pytest.fixture()
def state_with_smtp(smtp_relation: testing.Relation) -> testing.State:
    """Provide a state with SMTP relation."""
    return testing.State(relations=[smtp_relation], leader=True)


@pytest.fixture()
def base_state() -> testing.State:
    """Provide a state without SMTP relation."""
    return testing.State(leader=True)


@pytest.mark.usefixtures("mock_smtp_requires")
class TestHasRelation:
    """Tests for the Smtp.has_relation method."""

    def test_has_relation_when_relation_exists(
        self,
        ctx: testing.Context,
        state_with_smtp: testing.State,
    ):
        """Test that has_relation returns True when SMTP relation exists."""
        with ctx(ctx.on.update_status(), state_with_smtp) as mgr:
            assert mgr.charm.smtp.has_relation() is True

    def test_has_relation_when_no_relation(
        self,
        ctx: testing.Context,
        base_state: testing.State,
    ):
        """Test that has_relation returns False when SMTP relation does not exist."""
        with ctx(ctx.on.update_status(), base_state) as mgr:
            assert mgr.charm.smtp.has_relation() is False


class TestGetRelationData:
    """Tests for the Smtp.get_relation_data method."""

    @pytest.fixture()
    def valid_smtp_data(self) -> SmtpRelationData:
        """Provide valid SMTP relation data."""
        return SmtpRelationData(
            host="smtp.example.com",
            port=587,
            user="user@example.com",
            password="secret",  # nosec: B106
            auth_type=AuthType.PLAIN,
            transport_security=TransportSecurity.STARTTLS,
        )

    def test_valid_relation_data(
        self,
        ctx: testing.Context,
        state_with_smtp: testing.State,
        mock_smtp_requires: MockType,
        valid_smtp_data: SmtpRelationData,
    ):
        """Test that valid SMTP relation data is returned correctly."""
        mock_smtp_requires.get_relation_data.return_value = valid_smtp_data

        with ctx(ctx.on.update_status(), state_with_smtp) as mgr:
            data = mgr.charm.smtp.get_relation_data()

            assert data.host == "smtp.example.com"
            assert data.port == 587
            assert data.user == "user@example.com"
            assert data.auth_type == AuthType.PLAIN
            assert data.transport_security == TransportSecurity.STARTTLS

    def test_relation_data_none_raises_waiting(
        self,
        ctx: testing.Context,
        state_with_smtp: testing.State,
        mock_smtp_requires: MockType,
    ):
        """Test that None relation data raises MediaWikiWaitingStatusException."""
        mock_smtp_requires.get_relation_data.return_value = None

        with (
            ctx(ctx.on.update_status(), state_with_smtp) as mgr,
            pytest.raises(
                MediaWikiWaitingStatusException,
                match="Waiting for smtp relation data",
            ),
        ):
            mgr.charm.smtp.get_relation_data()

    def test_relation_data_exception_raises_blocked(
        self,
        ctx: testing.Context,
        state_with_smtp: testing.State,
        mock_smtp_requires: MockType,
    ):
        """Test that an exception from the library raises MediaWikiBlockedStatusException."""
        mock_smtp_requires.get_relation_data.side_effect = RuntimeError("connection failed")

        with (
            ctx(ctx.on.update_status(), state_with_smtp) as mgr,
            pytest.raises(
                MediaWikiBlockedStatusException,
                match="Error fetching smtp relation data",
            ),
        ):
            mgr.charm.smtp.get_relation_data()
