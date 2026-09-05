# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for certificate transfer reconciliation."""

import pytest

from certificate_transfer import CertificateTransfer
from exceptions import MediaWikiBlockedStatusException, MediaWikiWaitingStatusException
from types_ import CommandExecResult


def _reconciler(
    mocker, certificates: set[str], *, has_relation: bool = True, ready: bool = True
) -> CertificateTransfer:
    """Return a certificate transfer object with its relation mocked."""
    reconciler = object.__new__(CertificateTransfer)
    reconciler._container = mocker.Mock()
    reconciler._transfer = mocker.Mock()
    reconciler._transfer.get_all_certificates.return_value = certificates
    reconciler._transfer.relationship_name = "receive-ca-cert"
    relation = mocker.Mock(active=True)
    reconciler._transfer.model.relations = {"receive-ca-cert": [relation] if has_relation else []}
    reconciler._transfer.is_ready.return_value = ready
    reconciler._run_cli = mocker.Mock(return_value=CommandExecResult(0, "", ""))
    return reconciler


def test_build_bundle_sorts_unique_certificates() -> None:
    """The desired bundle contains unique certificates in stable order."""
    result = CertificateTransfer._build_bundle({"certificate-b", "certificate-a"})

    assert result == "certificate-a\ncertificate-b\n"


def test_reconcile_writes_bundle_and_refreshes_trust_store(mocker) -> None:
    """A changed bundle is written and imported into the system trust store."""
    path = mocker.Mock()
    path.exists.return_value = False
    mocker.patch("certificate_transfer.ContainerPath", return_value=path)
    ensure_contents = mocker.patch("certificate_transfer.ensure_contents", return_value=True)
    reconciler = _reconciler(mocker, {"certificate"})

    assert reconciler.reconcile() is True

    ensure_contents.assert_called_once_with(
        path,
        "certificate\n",
        mode=0o644,
        user="root",
        group="root",
    )
    reconciler._run_cli.assert_called_once_with(
        ["update-ca-certificates"], user="root", group="root"
    )


def test_reconcile_merges_additional_certificates(mocker) -> None:
    """Additional certificates are included in the managed trust bundle."""
    path = mocker.Mock()
    path.exists.return_value = False
    mocker.patch("certificate_transfer.ContainerPath", return_value=path)
    ensure_contents = mocker.patch("certificate_transfer.ensure_contents", return_value=True)
    reconciler = _reconciler(mocker, {"receive-ca"})

    assert reconciler.reconcile(additional_certificates=["valkey-ca"]) is True

    ensure_contents.assert_called_once_with(
        path,
        "receive-ca\nvalkey-ca\n",
        mode=0o644,
        user="root",
        group="root",
    )


def test_reconcile_deduplicates_additional_certificates(mocker) -> None:
    """A certificate supplied by both sources appears only once in the bundle."""
    path = mocker.Mock()
    path.exists.return_value = False
    mocker.patch("certificate_transfer.ContainerPath", return_value=path)
    ensure_contents = mocker.patch("certificate_transfer.ensure_contents", return_value=True)
    reconciler = _reconciler(mocker, {"certificate"})

    assert reconciler.reconcile(additional_certificates=["certificate"]) is True

    assert ensure_contents.call_args.args[1] == "certificate\n"


def test_reconcile_removes_valkey_certificate_but_keeps_transferred(mocker) -> None:
    """Removing an additional certificate preserves transferred certificates."""
    path = mocker.Mock()
    path.exists.return_value = True
    path.read_text.return_value = "receive-ca\nvalkey-ca\n"
    mocker.patch("certificate_transfer.ContainerPath", return_value=path)
    ensure_contents = mocker.patch("certificate_transfer.ensure_contents", return_value=True)
    reconciler = _reconciler(mocker, {"receive-ca"})

    assert reconciler.reconcile() is True

    assert ensure_contents.call_args.args[1] == "receive-ca\n"


def test_reconcile_does_not_refresh_unchanged_bundle(mocker) -> None:
    """An unchanged bundle does not trigger a trust-store refresh."""
    path = mocker.Mock()
    path.exists.return_value = True
    path.read_text.return_value = "certificate\n"
    mocker.patch("certificate_transfer.ContainerPath", return_value=path)
    mocker.patch("certificate_transfer.ensure_contents", return_value=False)
    reconciler = _reconciler(mocker, {"certificate"})

    assert reconciler.reconcile() is False

    reconciler._run_cli.assert_not_called()


def test_reconcile_removes_bundle_without_active_relation(mocker) -> None:
    """Removing the last relation removes the managed bundle and refreshes trust."""
    path = mocker.Mock()
    path.exists.return_value = True
    mocker.patch("certificate_transfer.ContainerPath", return_value=path)
    reconciler = _reconciler(mocker, set(), has_relation=False)

    assert reconciler.reconcile() is True

    reconciler._transfer.get_all_certificates.assert_called_once_with()
    path.unlink.assert_called_once_with()
    reconciler._run_cli.assert_called_once_with(
        ["update-ca-certificates"], user="root", group="root"
    )


def test_reconcile_waits_for_relation_data(mocker) -> None:
    """An active unready relation keeps the last bundle and waits."""
    pathops = mocker.patch("certificate_transfer.ContainerPath")
    reconciler = _reconciler(mocker, {"certificate"}, ready=False)

    with pytest.raises(MediaWikiWaitingStatusException, match="not ready"):
        reconciler.reconcile()

    pathops.assert_not_called()
    reconciler._transfer.get_all_certificates.assert_not_called()
    reconciler._transfer.is_ready.assert_called_once()
    reconciler._run_cli.assert_not_called()


def test_reconcile_keeps_last_good_bundle_when_refresh_fails(mocker) -> None:
    """A failed refresh restores the previous bundle before blocking."""
    path = mocker.Mock()
    path.exists.return_value = True
    path.read_text.return_value = "previous bundle\n"
    mocker.patch("certificate_transfer.ContainerPath", return_value=path)
    ensure_contents = mocker.patch(
        "certificate_transfer.ensure_contents", side_effect=[True, True]
    )
    reconciler = _reconciler(mocker, {"certificate"})
    reconciler._run_cli.side_effect = [
        CommandExecResult(1, "", "failed"),
        CommandExecResult(0, "", ""),
    ]

    with pytest.raises(
        MediaWikiBlockedStatusException,
        match="Updating the workload CA certificate store failed",
    ):
        reconciler.reconcile()

    assert ensure_contents.call_args_list[1].args[1] == "previous bundle\n"
    assert reconciler._run_cli.call_count == 2
