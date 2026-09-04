# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for outbound egress route resolution."""

import pytest

from egress import ProxyRouteResolver, TunnelServiceRegistry
from state import ProxyConfig


class TestTunnelServiceRegistry:
    """Tests for proxy route resolution and tunnel service registration."""

    @staticmethod
    def _registry(proxy: ProxyConfig | None) -> TunnelServiceRegistry:
        return TunnelServiceRegistry(ProxyRouteResolver(proxy))

    def test_register_is_direct_without_proxy(self) -> None:
        uses_tunnel = self._registry(None).register("smtp-proxy", 8125, "mail.example.com", 587)

        assert not uses_tunnel

    def test_tcp_route_is_direct_for_no_proxy_host(self) -> None:
        proxy = ProxyConfig(
            http_proxy="http://proxy.example.com:3128",  # type: ignore[arg-type]
            https_proxy=None,
            no_proxy="mail.example.com",
        )

        uses_tunnel = self._registry(proxy).register("smtp-proxy", 8125, "mail.example.com", 587)

        assert not uses_tunnel

    def test_tcp_route_normalizes_scheme_qualified_host(self) -> None:
        proxy = ProxyConfig(
            http_proxy="http://proxy.example.com:3128",  # type: ignore[arg-type]
            https_proxy=None,
            no_proxy="mail.example.com",
        )

        uses_tunnel = self._registry(proxy).register(
            "smtp-proxy", 8125, "smtp://mail.example.com", 587
        )

        assert not uses_tunnel

    def test_tcp_route_is_direct_for_no_proxy_cidr(self) -> None:
        proxy = ProxyConfig(
            http_proxy="http://proxy.example.com:3128",  # type: ignore[arg-type]
            https_proxy=None,
            no_proxy="10.151.0.0/16",
        )

        uses_tunnel = self._registry(proxy).register("smtp-proxy", 8125, "10.151.1.2", 587)

        assert not uses_tunnel

    def test_tcp_route_uses_http_connect_proxy(self) -> None:
        proxy = ProxyConfig(
            http_proxy="http://proxy.example.com:3128",  # type: ignore[arg-type]
            https_proxy=None,
            no_proxy=None,
        )

        uses_tunnel = self._registry(proxy).register("smtp-proxy", 8125, "mail.example.com", 587)

        assert uses_tunnel

    def test_register_stores_http_connect_tunnel_command(self) -> None:
        proxy = ProxyConfig(
            http_proxy="http://proxy.example.com:3128",  # type: ignore[arg-type]
            https_proxy=None,
            no_proxy=None,
        )
        registry = self._registry(proxy)

        assert registry.register("smtp-proxy", 8125, "mail.example.com", 587)
        assert registry.pebble_services()["smtp-proxy"].get("command") == (
            "/usr/bin/socat TCP-LISTEN:8125,bind=127.0.0.1,reuseaddr,fork,keepalive "
            "PROXY:proxy.example.com:mail.example.com:587,proxyport=3128,keepalive"
        )

    def test_tcp_route_uses_https_proxy_when_http_proxy_is_unset(self) -> None:
        proxy = ProxyConfig(
            http_proxy=None,
            https_proxy="http://secure-proxy.example.com:8443",  # type: ignore[arg-type]
            no_proxy=None,
        )

        registry = self._registry(proxy)

        assert registry.register("smtp-proxy", 8125, "mail.example.com", 587)
        assert registry.pebble_services()["smtp-proxy"].get("command") == (
            "/usr/bin/socat TCP-LISTEN:8125,bind=127.0.0.1,reuseaddr,fork,keepalive "
            "PROXY:secure-proxy.example.com:mail.example.com:587,proxyport=8443,keepalive"
        )

    def test_tcp_route_prefers_https_proxy_over_http_proxy(self) -> None:
        proxy = ProxyConfig(
            http_proxy="http://proxy.example.com:3128",  # type: ignore[arg-type]
            https_proxy="http://secure-proxy.example.com:8443",  # type: ignore[arg-type]
            no_proxy=None,
        )

        registry = self._registry(proxy)

        assert registry.register("smtp-proxy", 8125, "mail.example.com", 587)
        assert (
            "PROXY:secure-proxy.example.com:mail.example.com:587,proxyport=8443"
            in registry.pebble_services()["smtp-proxy"].get("command", "")
        )

    def test_tcp_route_can_prefer_http_proxy(self) -> None:
        proxy = ProxyConfig(
            http_proxy="http://proxy.example.com:3128",  # type: ignore[arg-type]
            https_proxy="http://secure-proxy.example.com:8443",  # type: ignore[arg-type]
            no_proxy=None,
        )
        registry = self._registry(proxy)
        assert registry.register(
            "smtp-proxy", 8125, "mail.example.com", 587, prefer_http_proxy=True
        )

        assert (
            "PROXY:proxy.example.com:mail.example.com:587,proxyport=3128"
            in registry.pebble_services()["smtp-proxy"].get("command", "")
        )

    def test_tcp_route_uses_http_proxy_scheme_default_port(self) -> None:
        proxy = ProxyConfig(
            http_proxy="http://proxy.example.com",  # type: ignore[arg-type]
            https_proxy=None,
            no_proxy=None,
        )

        registry = self._registry(proxy)

        assert registry.register("smtp-proxy", 8125, "mail.example.com", 587)
        assert "proxyport=80" in registry.pebble_services()["smtp-proxy"].get("command", "")

    @pytest.mark.parametrize(
        ("proxy_url", "expected_port"),
        [("https://proxy.example.com", 443), ("https://proxy.example.com:8443", 8443)],
    )
    def test_tcp_route_warns_for_https_proxy(
        self, caplog: pytest.LogCaptureFixture, proxy_url: str, expected_port: int
    ) -> None:
        proxy = ProxyConfig(
            http_proxy=proxy_url,  # type: ignore[arg-type]
            https_proxy=None,
            no_proxy=None,
        )

        registry = self._registry(proxy)

        assert registry.register("smtp-proxy", 8125, "mail.example.com", 587)
        assert f"proxyport={expected_port}" in registry.pebble_services()["smtp-proxy"].get(
            "command", ""
        )
        assert any("uses the https:// scheme" in message for message in caplog.messages)

    @pytest.mark.parametrize(
        "no_proxy",
        ["*", ".example.com", "mail.example.com:587"],
    )
    def test_tcp_route_honors_standard_no_proxy_entries(self, no_proxy: str) -> None:
        proxy = ProxyConfig(
            http_proxy="http://proxy.example.com:3128",  # type: ignore[arg-type]
            https_proxy=None,
            no_proxy=no_proxy,
        )

        uses_tunnel = self._registry(proxy).register("smtp-proxy", 8125, "mail.example.com", 587)

        assert not uses_tunnel

    def test_register_rejects_partial_destination(self) -> None:
        with pytest.raises(ValueError, match="host and port must be provided together"):
            self._registry(None).register("smtp-proxy", 8125, "mail.example.com")

    def test_latest_registration_replaces_service_definition(self) -> None:
        proxy = ProxyConfig(
            http_proxy="http://proxy.example.com:3128",  # type: ignore[arg-type]
            https_proxy=None,
            no_proxy=None,
        )
        registry = self._registry(proxy)

        registry.register("smtp-proxy", 8125, "mail.example.com", 587)
        assert registry.pebble_services()["smtp-proxy"].get("startup") == "enabled"

        registry.register("smtp-proxy", 8126)
        assert registry.pebble_services()["smtp-proxy"].get("startup") == "disabled"

    def test_ssh_proxy_command_honors_no_proxy(self) -> None:
        proxy = ProxyConfig(
            http_proxy="http://proxy.example.com:3128",  # type: ignore[arg-type]
            https_proxy=None,
            no_proxy="git.example.com",
        )

        resolver = ProxyRouteResolver(proxy)

        assert resolver.proxy_command("git.example.com", 22) is None
        assert resolver.proxy_command("other.example.com", 22) == (
            "/usr/bin/socat - PROXY:proxy.example.com:other.example.com:22,proxyport=3128"
        )

    def test_ssh_proxy_command_uses_ssh_placeholders(self) -> None:
        proxy = ProxyConfig(
            http_proxy="http://proxy.example.com:3128",  # type: ignore[arg-type]
            https_proxy=None,
            no_proxy="git.example.com",
        )

        resolver = ProxyRouteResolver(proxy)

        assert resolver.ssh_proxy_command() == (
            "/usr/bin/socat - PROXY:proxy.example.com:%h:%p,proxyport=3128"
        )
