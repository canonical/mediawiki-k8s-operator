# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Secrets that need to be synced between MediaWiki units."""

import dataclasses
import secrets


@dataclasses.dataclass(frozen=True)
class MediaWikiSecrets:
    """A dataclass to hold secrets relevant to MediaWiki that need to be synced between units."""

    secret_key: str
    session_secret: str
    saml_secret_salt: str

    @classmethod
    def generate(cls) -> "MediaWikiSecrets":
        """Returns a new instance of MediaWikiSecrets with randomly generated secrets."""
        return cls(
            secret_key=secrets.token_urlsafe(64),
            session_secret=secrets.token_urlsafe(64),
            saml_secret_salt=secrets.token_hex(64),
        )

    def to_local_settings(self) -> dict[str, str]:
        """Return the secrets formatted as a dictionary of PHP variable assignments to be included in LateSettings.php."""
        return {
            "$wgSecretKey": self.secret_key,
            "$wgSessionSecret": self.session_secret,
        }

    def to_juju_secret(self) -> dict[str, str]:
        """Return the secrets formatted as a dictionary for storing in Juju secrets."""
        # Juju secrets restricts key names to lowercase alphanumerics and dashes.
        return {
            "key": self.secret_key,
            "session": self.session_secret,
            "saml-salt": self.saml_secret_salt,
        }

    @classmethod
    def from_juju_secret(cls, data: dict[str, str]) -> "MediaWikiSecrets":
        """Create an instance of MediaWikiSecrets from a Juju secret style dictionary."""
        return cls(
            secret_key=data["key"],
            session_secret=data["session"],
            saml_secret_salt=data["saml-salt"],
        )
