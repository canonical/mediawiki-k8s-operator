# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Resolve outbound workload routes using the Juju model proxy configuration."""

from __future__ import annotations

import dataclasses
import logging
import shlex
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from ops import pebble
from requests.utils import should_bypass_proxies

if TYPE_CHECKING:
    from state import ProxyConfig


logger = logging.getLogger(__name__)


class ProxyRouteResolver:
    """Resolve TCP destinations according to model proxy settings."""

    def __init__(self, proxy_config: ProxyConfig | None) -> None:
        """Initialize the router.

        Args:
            proxy_config: Proxy settings supplied by the Juju model.
        """
        self._proxy_config = proxy_config

    def tunnel_command(
        self,
        local_port: int,
        host: str,
        port: int,
        *,
        prefer_http_proxy: bool = False,
    ) -> str | None:
        """Return a local HTTP CONNECT tunnel command when the destination needs a proxy.

        Args:
            local_port: Port used by the local tunnel listener.
            host: Original destination hostname or address.
            port: Original destination port.
            prefer_http_proxy: Whether to choose HTTP_PROXY before HTTPS_PROXY.

        Returns:
            The socat command, or None if the destination is direct.
        """
        host = urlparse(host).hostname or host
        if not self._should_proxy(host, port, prefer_http_proxy):
            return None
        return self._tunnel_command(host, port, local_port, prefer_http_proxy)

    def proxy_command(self, host: str, port: int) -> str | None:
        """Return a standalone HTTP CONNECT command when the destination needs a proxy."""
        if not self._should_proxy(host, port):
            return None
        return self._connect_command(host, port)

    def _should_proxy(self, host: str, port: int, prefer_http_proxy: bool = False) -> bool:
        """Return whether a TCP destination should use the HTTP proxy.

        Args:
            host: Normalized destination hostname or address without scheme.
            port: Destination port.
            prefer_http_proxy: Whether to choose HTTP_PROXY before HTTPS_PROXY.

        Returns:
            True if the destination should use the HTTP proxy, False otherwise.
        """
        if self._proxy_config is None or self._proxy(prefer_http_proxy) is None:
            return False

        netloc = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        destination = urlparse(f"//{netloc}", scheme="http").geturl()
        return not should_bypass_proxies(destination, self._proxy_config.no_proxy)

    def _proxy(self, prefer_http_proxy: bool = False) -> str | None:
        """Return the proxy to use for an HTTP CONNECT request."""
        if self._proxy_config is None:
            return None
        if prefer_http_proxy:
            return self._proxy_config.http_proxy_string or self._proxy_config.https_proxy_string
        return self._proxy_config.https_proxy_string or self._proxy_config.http_proxy_string

    def _proxy_endpoint(self, prefer_http_proxy: bool = False) -> tuple[str, int]:
        """Return the configured HTTP proxy endpoint."""
        proxy = self._proxy(prefer_http_proxy)
        if proxy is None:
            raise RuntimeError("HTTP proxy is required for a proxied TCP route")

        parsed_proxy = urlparse(proxy)
        if parsed_proxy.scheme == "https":
            logger.warning(
                "TCP proxy %s uses the https:// scheme; assuming it accepts plaintext HTTP CONNECT requests.",
                proxy,
            )
        proxy_port = parsed_proxy.port
        if proxy_port is None:
            proxy_port = 443 if parsed_proxy.scheme == "https" else 80

        return (
            parsed_proxy.hostname or "",
            proxy_port,
        )

    def _tunnel_command(
        self, host: str, port: int, local_port: int, prefer_http_proxy: bool
    ) -> str:
        """Build a local TCP listener that forwards through HTTP CONNECT."""
        proxy_host, proxy_port = self._proxy_endpoint(prefer_http_proxy)
        proxy_target = f"PROXY:{proxy_host}:{host}:{port},proxyport={proxy_port},keepalive"
        return " ".join(
            (
                "socat",
                f"TCP-LISTEN:{local_port},bind=127.0.0.1,reuseaddr,fork,keepalive",
                shlex.quote(proxy_target),
            )
        )

    def _connect_command(self, host: str, port: int) -> str:
        """Build a standalone socat HTTP CONNECT command."""
        proxy_host, proxy_port = self._proxy_endpoint()
        return (
            f"socat - PROXY:{shlex.quote(proxy_host)}:{shlex.quote(host)}:{port},"
            f"proxyport={proxy_port}"
        )


@dataclasses.dataclass(frozen=True)
class _TunnelRegistration:
    """A caller-provided local tunnel service definition."""

    service_name: str
    local_port: int
    tunnel_command: str | None


class TunnelServiceRegistry:
    """Register local tunnels and render their Pebble service definitions."""

    def __init__(self, route_resolver: ProxyRouteResolver) -> None:
        """Initialize the registry.

        Args:
            route_resolver: Resolves destination routes through the model proxy.
        """
        self._route_resolver = route_resolver
        self._registrations: dict[str, _TunnelRegistration] = {}

    def register(
        self,
        service_name: str,
        local_port: int,
        host: str | None = None,
        port: int | None = None,
        *,
        prefer_http_proxy: bool = False,
    ) -> bool:
        """Register a tunnel service, replacing any earlier registration with its name.

        Args:
            service_name: Name of the Pebble service that runs the tunnel.
            local_port: Port used by the local tunnel listener.
            host: Optional destination hostname or address.
            port: Optional destination port.
            prefer_http_proxy: Whether to choose HTTP_PROXY before HTTPS_PROXY.

        Returns:
            Whether the destination requires a local tunnel.

        Raises:
            ValueError: If only one of host and port is provided.
        """
        if (host is None) != (port is None):
            raise ValueError("Tunnel host and port must be provided together")

        tunnel_command = (
            self._route_resolver.tunnel_command(
                local_port, host, port, prefer_http_proxy=prefer_http_proxy
            )
            if host is not None and port is not None
            else None
        )
        self._registrations[service_name] = _TunnelRegistration(
            service_name, local_port, tunnel_command
        )
        return tunnel_command is not None

    def pebble_services(self) -> dict[str, pebble.ServiceDict]:
        """Return Pebble service definitions for all registered tunnels."""
        return {
            service_name: self._pebble_service(registration)
            for service_name, registration in self._registrations.items()
        }

    @staticmethod
    def _pebble_service(registration: _TunnelRegistration) -> pebble.ServiceDict:
        """Return the Pebble service definition for one tunnel registration."""
        if registration.tunnel_command is not None:
            return {
                "override": "replace",
                "summary": "HTTP CONNECT proxy tunnel",
                "command": registration.tunnel_command,
                "startup": "enabled",
            }
        return {
            "override": "replace",
            "summary": "Disabled HTTP CONNECT proxy tunnel",
            "startup": "disabled",
        }
