# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Receive CA certificates and install them in the workload trust store."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Iterable

from charmlibs.pathops import ContainerPath, ensure_contents
from charms.certificate_transfer_interface.v1.certificate_transfer import (
    CertificateTransferRequirerCharmEvents,
    CertificateTransferRequires,
)
from ops import Container, Object

from container import ContainerService
from exceptions import MediaWikiBlockedStatusException, MediaWikiWaitingStatusException

if TYPE_CHECKING:
    from state import StatefulCharmBase

logger = logging.getLogger(__name__)


class CertificateTransfer(ContainerService, Object):
    """Manage CA certificates received through the certificate transfer relation."""

    CA_CERTIFICATE_PATH = "/usr/local/share/ca-certificates/certificate-transfer-ca.crt"
    _FILE_MODE = 0o644
    _FILE_USER = "root"
    _FILE_GROUP = "root"

    def __init__(self, charm: StatefulCharmBase, relation_name: str, container: Container):
        """Initialize the certificate transfer requirer.

        Args:
            charm: The parent charm.
            relation_name: The certificate transfer relation endpoint name.
            container: The workload container receiving the certificates.
        """
        Object.__init__(self, charm, "receive-ca-cert-observer")
        ContainerService.__init__(self, container)
        self._transfer = CertificateTransferRequires(charm, relation_name)

    @property
    def on(self) -> CertificateTransferRequirerCharmEvents:
        """Return certificate transfer relation events."""
        return self._transfer.on

    def _get_certificates(self) -> Iterable[str]:
        """Return all transferred certificates after checking relation readiness."""
        relations = [
            relation
            for relation in self._transfer.model.relations[self._transfer.relationship_name]
            if relation.active
        ]
        if any(not self._transfer.is_ready(relation) for relation in relations):
            raise MediaWikiWaitingStatusException("Certificate transfer relation is not ready")
        return self._transfer.get_all_certificates()

    def reconcile(self) -> bool:
        """Install received CA certificates and refresh the system trust store.

        Returns:
            Whether the managed trust bundle changed.

        Raises:
            MediaWikiBlockedStatusException: If the relation data cannot be read,
                the bundle cannot be installed, or the system trust store cannot be
                refreshed.
            MediaWikiWaitingStatusException: If an active relation is not ready.
        """
        desired_bundle = self._build_bundle(self._get_certificates())

        try:
            path = ContainerPath(self.CA_CERTIFICATE_PATH, container=self._container)
            previous_bundle = path.read_text() if path.exists() else None
        except Exception as error:
            raise MediaWikiBlockedStatusException(
                f"Failed to read the workload CA certificate bundle: {error}"
            ) from error
        try:
            changed = self._write_bundle(path, desired_bundle)
        except Exception as error:
            self._restore_bundle(path, previous_bundle)
            raise MediaWikiBlockedStatusException(
                f"Failed to install the workload CA certificate bundle: {error}"
            ) from error
        if not changed:
            return False

        try:
            self._refresh_trust_store()
        except MediaWikiBlockedStatusException:
            self._restore_bundle(path, previous_bundle)
            try:
                self._refresh_trust_store()
            except MediaWikiBlockedStatusException:
                logger.exception("Failed to restore the workload CA certificate store")
            raise
        return True

    def _restore_bundle(self, path: ContainerPath, content: str | None) -> None:
        """Restore the last known-good bundle, logging any failure."""
        try:
            self._write_bundle(path, content)
        except Exception:
            logger.exception("Failed to restore the workload CA certificate bundle")

    def _refresh_trust_store(self) -> None:
        """Refresh the system trust store."""
        self._run_cli(
            ["update-ca-certificates"], user=self._FILE_USER, group=self._FILE_GROUP
        ).raise_for_status(
            "Updating the workload CA certificate store", MediaWikiBlockedStatusException
        )

    @classmethod
    def _build_bundle(cls, certificates: Iterable[str]) -> str:
        """Return a deterministic PEM bundle from the relation certificates."""
        certificates = [certificate.rstrip() for certificate in sorted(set(certificates))]
        return "\n".join(certificates) + ("\n" if certificates else "")

    @classmethod
    def _write_bundle(cls, path: ContainerPath, content: str | None) -> bool:
        """Write or remove the charm-owned CA bundle and return whether it changed."""
        if content:
            return ensure_contents(
                path,
                content,
                mode=cls._FILE_MODE,
                user=cls._FILE_USER,
                group=cls._FILE_GROUP,
            )
        if path.exists():
            path.unlink()
            return True
        return False
