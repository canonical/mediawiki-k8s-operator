# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Manage TLS certificate requests for the MediaWiki workload."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from charmlibs.interfaces.tls_certificates import (
    CertificateRequestAttributes,
    Mode,
    TLSCertificatesRequiresV4,
)
from charmlibs.pathops import ContainerPath, ensure_contents
from ops import Container, Object

from exceptions import MediaWikiBlockedStatusException

if TYPE_CHECKING:
    from state import StatefulCharmBase


@dataclass(frozen=True)
class TlsReconciliationResult:
    """Result of reconciling TLS material into a workload container."""

    ready: bool
    changed: bool


class Tls(Object):
    """Manage the TLS certificates relation."""

    CERTIFICATE_PATH = "/etc/mediawiki/tls/certificate.pem"
    PRIVATE_KEY_PATH = "/etc/mediawiki/tls/private-key.pem"
    _FILE_MODE = 0o640
    _FILE_USER = "root"
    _FILE_GROUP = "_daemon_"

    def __init__(self, charm: StatefulCharmBase, relation_name: str):
        """Initialize the TLS certificates requirer.

        Args:
            charm: The parent charm.
            relation_name: The tls-certificates relation endpoint name.
        """
        super().__init__(charm, "tls-observer")

        self._charm = charm
        self._relation_name = relation_name
        service_hostname = f"{charm.app.name}-endpoints.{charm.model.name}.svc.cluster.local"
        self._certificate_request = CertificateRequestAttributes(
            common_name=service_hostname,
            sans_dns={service_hostname},
        )
        self.tls = TLSCertificatesRequiresV4(
            charm=charm,
            relationship_name=relation_name,
            certificate_requests=[self._certificate_request],
            mode=Mode.UNIT,
        )

    @property
    def relation_name(self) -> str:
        """Return the TLS certificates relation name."""
        return self._relation_name

    def has_relation(self) -> bool:
        """Return whether the TLS certificates relation is established."""
        return self.model.get_relation(self.relation_name) is not None

    def get_material(self) -> tuple[str, str] | None:
        """Return the assigned certificate chain and private key.

        Returns:
            The certificate chain and private key, or None when the certificates relation is absent
            or certificate material has not been issued yet.

        Raises:
            MediaWikiBlockedStatusException: If certificate issuance was denied.
        """
        if not self.has_relation():
            return None

        errors = self.tls.get_request_errors()
        if errors:
            raise MediaWikiBlockedStatusException(
                f"TLS certificate request denied: {errors[0].error.message}"
            )

        certificate, private_key = self.tls.get_assigned_certificate(self._certificate_request)
        if certificate is None or private_key is None:
            return None

        certificate_chain = "\n".join(
            item.raw for item in [certificate.certificate, *certificate.chain]
        )
        return certificate_chain, private_key.raw

    def is_ready(self) -> bool:
        """Return whether assigned certificate material is available."""
        return self.get_material() is not None

    def reconcile(self, container: Container) -> TlsReconciliationResult:
        """Write or remove assigned TLS material in a workload container.

        Args:
            container: The workload container that consumes the TLS material.

        Returns:
            The resulting material readiness and whether files changed.
        """
        material = self.get_material()
        if material is None:
            return TlsReconciliationResult(
                ready=False,
                changed=self._remove_material(container),
            )

        certificate_chain, private_key = material
        changed = ensure_contents(
            ContainerPath(self.CERTIFICATE_PATH, container=container),
            certificate_chain,
            mode=self._FILE_MODE,
            user=self._FILE_USER,
            group=self._FILE_GROUP,
        )
        changed |= ensure_contents(
            ContainerPath(self.PRIVATE_KEY_PATH, container=container),
            private_key,
            mode=self._FILE_MODE,
            user=self._FILE_USER,
            group=self._FILE_GROUP,
        )
        return TlsReconciliationResult(ready=True, changed=changed)

    @classmethod
    def _remove_material(cls, container: Container) -> bool:
        """Remove charm-owned TLS material from a workload container."""
        changed = False
        for path in (cls.CERTIFICATE_PATH, cls.PRIVATE_KEY_PATH):
            file = ContainerPath(path, container=container)
            if file.exists():
                file.unlink()
                changed = True
        return changed
