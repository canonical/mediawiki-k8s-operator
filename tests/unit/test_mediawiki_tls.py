# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for MediaWiki Apache TLS site management."""

from mediawiki._tls import _TlsMixin
from tls import TlsReconciliationResult


class MediaWikiTls(_TlsMixin):
    """Concrete TLS mixin for unit tests."""


def test_enables_vendored_apache_tls_site(mocker) -> None:
    """The TLS site is enabled with Apache's site helper when it is disabled."""
    enabled_site = mocker.Mock()
    enabled_site.is_symlink.return_value = False
    mocker.patch("mediawiki._tls.ContainerPath", return_value=enabled_site)
    container = mocker.Mock()

    changed = _TlsMixin._set_apache_site_enabled(container, enabled=True)

    assert changed is True
    container.exec.assert_called_once_with(["/usr/sbin/a2ensite", "mediawiki-tls"])


def test_disables_vendored_apache_tls_site(mocker) -> None:
    """The TLS site is disabled with Apache's site helper when it is enabled."""
    enabled_site = mocker.Mock()
    enabled_site.is_symlink.return_value = True
    mocker.patch("mediawiki._tls.ContainerPath", return_value=enabled_site)
    container = mocker.Mock()

    changed = _TlsMixin._set_apache_site_enabled(container, enabled=False)

    assert changed is True
    container.exec.assert_called_once_with(["/usr/sbin/a2dissite", "mediawiki-tls"])


def test_tls_reconciliation_combines_material_and_site_changes(mocker) -> None:
    """Material or Apache site changes require an Apache restart."""
    container = mocker.Mock()
    tls = mocker.Mock()
    tls.reconcile.return_value = TlsReconciliationResult(ready=True, changed=False)
    mediawiki = MediaWikiTls(container)
    mediawiki._tls = tls
    mocker.patch.object(mediawiki, "_set_apache_site_enabled", return_value=True)

    changed = mediawiki._tls_reconciliation()

    assert changed is True
    tls.reconcile.assert_called_once_with(container)
    mediawiki._set_apache_site_enabled.assert_called_once_with(container, enabled=True)
