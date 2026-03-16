# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""MediaWiki charm state."""

import dataclasses
import ipaddress
import json
import logging
import os
import re
import typing
from typing import Any, Literal, Optional, TypeVar
from urllib.parse import urlparse

import ops

# pylint: disable=no-name-in-module
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError, field_validator

from exceptions import CharmConfigInvalidError

logger = logging.getLogger(__name__)


class CharmConfig(BaseModel):
    """Configuration schema for the MediaWiki charm, reflecting charmcraft.yaml."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    composer: dict[str, Any] = Field(default_factory=dict)
    ssh_key: Optional[ops.Secret] = Field(None)
    static_assets_git_repo: str = Field("")
    static_assets_git_ref: str = Field("")
    hostname: str = Field("")
    local_settings: str = Field("")
    robots_txt: str = Field("")

    @field_validator("composer", mode="before")
    @classmethod
    def validate_composer(cls, v: Any) -> dict[str, Any]:
        """Validate that the composer config is valid JSON and a dictionary."""
        if not isinstance(v, str):
            raise ValueError("Composer configuration must be a string containing a JSON object")

        if not v:
            return {}
        try:
            curr = json.loads(v)
            if not isinstance(curr, dict):
                raise ValueError("Composer configuration must be a JSON object")
            return curr
        except json.JSONDecodeError as e:
            raise ValueError(f"Composer configuration must be a JSON object: {e}")

    @field_validator("hostname")
    @classmethod
    def validate_hostname(cls, v: str) -> str:
        """Validate that hostname is sensible. This is a relatively permissive validation.

        There should be no schema in the hostname, but it can be an IP address, DNS name, and may include a port.
        """
        if not v:
            return ""

        if "/" in v:
            raise ValueError("Hostname should not have a schema or path component")

        try:
            # The input shouldn't have a schema, so we add a dummy one to make urlparse work
            parsed = urlparse(f"http://{v}")
            hostname = parsed.hostname
            port = parsed.port  # Fails if port is present but not a valid 0-65535 integer
        except Exception as e:
            raise ValueError(f"Failed to validate hostname {e}")
        if not hostname:
            raise ValueError("Hostname cannot be empty")

        if port is not None and not (0 <= port <= 65535):
            raise ValueError("Hostname port number must be between 0 and 65535")

        try:
            ipaddress.ip_address(hostname)
            return v.strip()
        except ValueError:
            # Might be a DNS name still
            pass
        # Regex adapted from: https://stackoverflow.com/questions/106179/regular-expression-to-match-dns-hostname-or-ip-address
        hostname_regex = re.compile(
            r"^(([a-zA-Z0-9]|[a-zA-Z0-9][a-zA-Z0-9\-]*[a-zA-Z0-9])\.)*([A-Za-z0-9]|[A-Za-z0-9][A-Za-z0-9\-]*[A-Za-z0-9])$"
        )

        if not hostname_regex.match(hostname):
            raise ValueError("Hostname is not a valid")

        return v.strip()


class ProxyConfig(BaseModel):
    """Configuration for external access through proxy.

    Attributes:
        http_proxy: The http proxy URL.
        https_proxy: The https proxy URL.
        no_proxy: Comma separated list of hostnames to bypass proxy.
    """

    http_proxy: Optional[HttpUrl]
    https_proxy: Optional[HttpUrl]
    no_proxy: Optional[str]

    @classmethod
    def from_env(cls) -> Optional["ProxyConfig"]:
        """Instantiate ProxyConfig from juju charm environment.

        Returns:
            ProxyConfig if proxy configuration is provided, None otherwise.
        """
        http_proxy = os.environ.get("JUJU_CHARM_HTTP_PROXY")
        https_proxy = os.environ.get("JUJU_CHARM_HTTPS_PROXY")
        no_proxy = os.environ.get("JUJU_CHARM_NO_PROXY")

        if not http_proxy and not https_proxy:
            return None

        return cls(
            http_proxy=typing.cast(HttpUrl, http_proxy) if http_proxy else None,
            https_proxy=typing.cast(HttpUrl, https_proxy) if https_proxy else None,
            no_proxy=no_proxy,
        )

    @property
    def http_proxy_string(self) -> Optional[str]:
        """Http proxy URL as a string, or None if not set."""
        return self._http_url_to_string(self.http_proxy)

    @property
    def https_proxy_string(self) -> Optional[str]:
        """Https proxy URL as a string, or None if not set."""
        return self._http_url_to_string(self.https_proxy)

    @staticmethod
    def _http_url_to_string(http_url: Optional[HttpUrl]) -> Optional[str]:
        """Convert a pydantic HttpUrl to a string, or return None if input is None. This drops everything but the scheme, host, and port (if present)."""
        if http_url is None:
            return None

        out = f"{http_url.scheme}://{http_url.host}"
        if http_url.port:
            out += f":{http_url.port}"
        return out

    @property
    def as_dict(self) -> dict[str, str]:
        """Return the proxy configuration as a dictionary for use in environment variables.

        May set:
        - HTTP_PROXY
        - CGI_HTTP_PROXY (for composer)
        - HTTPS_PROXY
        - NO_PROXY
        """
        out = {}
        if self.http_proxy_string:
            out["HTTP_PROXY"] = self.http_proxy_string
            out["CGI_HTTP_PROXY"] = self.http_proxy_string

        if self.https_proxy_string:
            out["HTTPS_PROXY"] = self.https_proxy_string
        if self.no_proxy:
            out["NO_PROXY"] = self.no_proxy

        return out


@dataclasses.dataclass(frozen=True)
class State:
    """The MediaWiki k8s operator charm state.

    Attributes:
        proxy_config: Proxy configuration.
    """

    proxy_config: Optional[ProxyConfig]

    @classmethod
    def from_charm(cls, _: ops.CharmBase) -> "State":
        """Initialize the state from charm.

        Returns:
            Current state of the charm.

        Raises:
            CharmConfigInvalidError: if invalid state values were encountered.
        """
        try:
            proxy_config = ProxyConfig.from_env()
        except ValidationError as exc:
            logger.error("Invalid juju model proxy configuration, %s", exc)
            raise CharmConfigInvalidError("Invalid model proxy configuration.") from exc

        return cls(
            proxy_config=proxy_config,
        )


_T = TypeVar("_T")


class StatefulCharmBase(ops.CharmBase):
    """Charm base class with state management."""

    def __init__(self, *args: typing.Any):
        super().__init__(*args)

        try:
            self.state = State.from_charm(self)
        except CharmConfigInvalidError as exc:
            self.unit.status = ops.BlockedStatus(exc.msg)
            return

    def load_charm_config(
        self,
        *args: Any,
        errors: Literal["raise", "blocked"] = "raise",
        **kwargs: Any,
    ) -> CharmConfig:
        """Load the config into an instance of the CharmConfig class.

        Returns:
            An instance of the CharmConfig class with the loaded configuration.
        """
        try:
            return super().load_config(CharmConfig, *args, errors=errors, **kwargs)
        except ValueError as e:
            logger.error("Charm configuration error: %s", e)
            raise CharmConfigInvalidError("Invalid charm configuration.") from e
