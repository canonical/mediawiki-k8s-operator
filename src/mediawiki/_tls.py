# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Apache TLS reconciliation for the MediaWiki workload container."""

from charmlibs.pathops import ContainerPath
from ops import Container

from mediawiki._base import _MediaWikiBase
from tls import Tls


class _TlsMixin(_MediaWikiBase):
    """Manage Apache TLS configuration and certificate material."""

    _tls: Tls
    _APACHE_SITE_NAME = "mediawiki-tls"
    _APACHE_ENABLED_SITE_FILE = "/etc/apache2/sites-enabled/mediawiki-tls.conf"

    def _tls_reconciliation(self) -> bool:
        """Reconcile TLS material and the Apache TLS site.

        Returns:
            Whether Apache must be restarted to apply TLS changes.
        """
        result = self._tls.reconcile(self._container)
        site_changed = self._set_apache_site_enabled(self._container, enabled=result.ready)
        return result.changed or site_changed

    @classmethod
    def _set_apache_site_enabled(cls, container: Container, *, enabled: bool) -> bool:
        """Enable or disable the vendored Apache TLS site.

        Args:
            container: The MediaWiki workload container.
            enabled: Whether the TLS site should be enabled.

        Returns:
            Whether the enabled-site symlink changed.
        """
        enabled_site = ContainerPath(cls._APACHE_ENABLED_SITE_FILE, container=container)
        if enabled_site.is_symlink() == enabled:
            return False

        command = "/usr/sbin/a2ensite" if enabled else "/usr/sbin/a2dissite"
        container.exec([command, cls._APACHE_SITE_NAME]).wait_output()
        return True
