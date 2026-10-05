# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Manage the private Shellbox sidecar."""

import secrets

from charmlibs.pathops import ContainerPath, ensure_contents
from ops import Object, pebble

import utils
from exceptions import MediaWikiWaitingStatusException
from state import StatefulCharmBase

SHELLBOX_URL = "http://127.0.0.1:8080/shellbox"


class Shellbox(Object):
    """Configure authenticated command execution in the unit's Shellbox container."""

    AUTHENTICATION_KEY_CONTENT_KEY = "shellbox-key"  # nosec: B105
    APACHE_EXPORTER_PORT = 9118
    PHP_FPM_EXPORTER_PORT = 9253
    # Included by the rock's PHP-FPM configuration. Pebble serves its plan to any user in the
    # container, so the key is passed through this root-only file rather than the service
    # environment.
    KEY_FILE = "/etc/shellbox/key.conf"  # nosec: B105
    _PHP_FPM_SERVICE = "php-fpm"
    _SERVICES = (_PHP_FPM_SERVICE, "apache2", "apache-exporter", "php-fpm-exporter")

    def __init__(self, charm: StatefulCharmBase) -> None:
        """Initialize the manager.

        Args:
            charm: The charm managing the Shellbox container.
        """
        super().__init__(charm, "shellbox-manager")
        self._charm = charm
        self._container = charm.unit.get_container("shellbox")

    def authentication_key(self) -> str:
        """Return the stable authentication key owned by this unit."""
        return utils.unit_secret_value(
            self._charm, self.AUTHENTICATION_KEY_CONTENT_KEY, lambda: secrets.token_hex(64)
        )

    def reconciliation(self) -> None:
        """Configure and start Shellbox with this unit's authentication key.

        Raises:
            MediaWikiWaitingStatusException: If the Shellbox container is unavailable.
        """
        if not self._container.can_connect():
            raise MediaWikiWaitingStatusException("Waiting for shellbox sidecar")
        key_changed = ensure_contents(
            ContainerPath(self.KEY_FILE, container=self._container),
            f"[shellbox]\nenv[SHELLBOX_SECRET_KEY] = {self.authentication_key()}\n",
            mode=0o600,
            user="root",
            group="root",
        )
        layer: pebble.LayerDict = {
            "services": {
                name: {"override": "merge", "startup": "enabled"} for name in self._SERVICES
            },
            "checks": {
                "shellbox": {
                    "override": "replace",
                    "level": "ready",
                    "http": {"url": f"{SHELLBOX_URL}/healthz"},
                    # PHP-FPM terminates requests after 180 seconds, so long commands can
                    # occupy every worker and delay health checks for that long. Only fail
                    # the check, which restarts the services, after a longer outage.
                    "period": "30s",
                    "timeout": "5s",
                    "threshold": 7,
                }
            },
        }
        self._container.add_layer("shellbox", layer, combine=True)
        if key_changed:
            # PHP-FPM reads the key only when it starts.
            self._container.restart(self._PHP_FPM_SERVICE)
        self._container.replan()
