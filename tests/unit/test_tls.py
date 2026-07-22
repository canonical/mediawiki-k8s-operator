# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for TLS material reconciliation."""

from tls import Tls


def test_reconcile_writes_assigned_material(mocker) -> None:
    """Assigned certificate material is written using standardized attributes."""
    container = mocker.Mock()
    mocker.patch.object(Tls, "get_material", return_value=("certificate", "private-key"))
    ensure_contents = mocker.patch("tls.ensure_contents", side_effect=[True, False])
    mocker.patch("tls.ContainerPath")
    tls = object.__new__(Tls)

    result = tls.reconcile(container)

    assert result.ready is True
    assert result.changed is True
    assert ensure_contents.call_count == 2
    for call in ensure_contents.call_args_list:
        assert call.kwargs == {
            "mode": 0o640,
            "user": "root",
            "group": "_daemon_",
        }


def test_reconcile_removes_material_when_unavailable(mocker) -> None:
    """Unavailable certificate material removes both standardized files."""
    container = mocker.Mock()
    mocker.patch.object(Tls, "get_material", return_value=None)
    certificate = mocker.Mock()
    certificate.exists.return_value = True
    private_key = mocker.Mock()
    private_key.exists.return_value = False
    container_path = mocker.patch("tls.ContainerPath", side_effect=[certificate, private_key])
    tls = object.__new__(Tls)

    result = tls.reconcile(container)

    assert result.ready is False
    assert result.changed is True
    assert container_path.call_count == 2
    certificate.unlink.assert_called_once_with()
    private_key.unlink.assert_not_called()
