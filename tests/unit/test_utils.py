# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from unittest.mock import MagicMock

import pytest
from pydantic import HttpUrl

from state import ProxyConfig
from utils import extract_remote, remote_in_known_hosts, ssh_reconcile_config


class TestExtractRemote:
    """Tests for extract_remote."""

    @pytest.mark.parametrize(
        "url, expected",
        [
            ("git@github.com:user/repo.git", "github.com"),
            ("git@gitlab.example.com:org/project.git", "gitlab.example.com"),
            ("ssh://git@github.com/org/repo.git", "github.com"),
            ("ssh://git@github.com:2222/org/repo.git", "github.com"),
            ("git+ssh://deploy@git.launchpad.net/project", "git.launchpad.net"),
            ("https://github.com/user/repo.git", None),
            ("http://github.com/user/repo.git", None),
            ("", None),
        ],
    )
    def test_extract_remote(self, url: str, expected: str | None) -> None:
        """Test that SSH remotes are extracted correctly."""
        assert extract_remote(url) == expected


class TestRemoteInKnownHosts:
    """Tests for remote_in_known_hosts."""

    _KNOWN_HOSTS = (
        "github.com ssh-rsa AAAAB3...\n# a comment\n\ngitlab.com ecdsa-sha2-nistp256 AAAAE2...\n"
    )

    def test_present(self) -> None:
        """Test that a known host is detected."""
        assert remote_in_known_hosts("github.com", self._KNOWN_HOSTS)

    def test_absent(self) -> None:
        """Test that an unknown host is not detected."""
        assert not remote_in_known_hosts("example.com", self._KNOWN_HOSTS)

    def test_empty(self) -> None:
        """Test with empty known hosts content."""
        assert not remote_in_known_hosts("github.com", "")

    def test_comment_not_matched(self) -> None:
        """Test that comment lines are ignored."""
        assert not remote_in_known_hosts("#", self._KNOWN_HOSTS)

    def test_partial_match_not_accepted(self) -> None:
        """Test that a substring of a hostname does not match."""
        assert not remote_in_known_hosts("github", self._KNOWN_HOSTS)

    def test_matches_host_in_comma_separated_host_list(self) -> None:
        """Test that a host is matched when present in a host list."""
        known_hosts = "github.com,140.82.121.3 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA..."
        assert remote_in_known_hosts("github.com", known_hosts)

    def test_matches_host_when_marker_present(self) -> None:
        """Test that host matching works when a marker field is present."""
        known_hosts = "@cert-authority github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA..."
        assert remote_in_known_hosts("github.com", known_hosts)

    def test_ignores_host_when_revoked_marker_present(self) -> None:
        """Test that @revoked entries are ignored for host matching."""
        known_hosts = "@revoked github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA..."
        assert not remote_in_known_hosts("github.com", known_hosts)

    def test_matches_non_revoked_host_when_revoked_entry_also_present(self) -> None:
        """Test that a valid non-revoked entry still matches."""
        known_hosts = (
            "@revoked github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA...\n"
            "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA..."
        )
        assert remote_in_known_hosts("github.com", known_hosts)

    def test_matches_bracketed_host_with_port(self) -> None:
        """Test that bracketed host:port entries match on host."""
        known_hosts = "[github.com]:2222 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA..."
        assert remote_in_known_hosts("github.com", known_hosts)

    def test_matches_host_from_marker_prefixed_comma_separated_list(self) -> None:
        """Test matching from marker-prefixed lines with host lists."""
        known_hosts = "@cert-authority github.com,[140.82.121.3]:2222 ssh-ed25519 AAAAC3Nza..."
        assert remote_in_known_hosts("github.com", known_hosts)

    def test_ignores_marker_only_line(self) -> None:
        """Test that malformed marker-only lines are ignored."""
        known_hosts = "@cert-authority\n"
        assert not remote_in_known_hosts("github.com", known_hosts)


class TestSshReconcileConfig:
    """Tests for ssh_reconcile_config."""

    @staticmethod
    def _make_path(exists: bool = True) -> MagicMock:
        """Create a mock ContainerPath."""
        path = MagicMock()
        path.exists.return_value = exists
        path.parent = MagicMock()
        return path

    def test_writes_key_when_provided(self) -> None:
        """Test that an SSH key is written when provided."""
        key_file = self._make_path(exists=False)
        config_file = self._make_path()
        known_hosts = self._make_path()

        ssh_reconcile_config(
            ssh_key="PRIVATE_KEY_CONTENT",
            key_file=key_file,
            config_file=config_file,
            known_hosts_file=known_hosts,
            known_hosts_content="github.com ssh-rsa AAA...",
            proxy_config=None,
        )

        key_file.write_text.assert_called_once_with("PRIVATE_KEY_CONTENT\n", mode=0o600)

    def test_removes_key_when_none(self) -> None:
        """Test that an existing SSH key is removed when ssh_key is None."""
        key_file = self._make_path(exists=True)
        config_file = self._make_path()
        known_hosts = self._make_path()

        ssh_reconcile_config(
            ssh_key=None,
            key_file=key_file,
            config_file=config_file,
            known_hosts_file=known_hosts,
            known_hosts_content="github.com ssh-rsa AAA...",
            proxy_config=None,
        )

        key_file.unlink.assert_called_once()

    def test_does_not_remove_key_when_absent(self) -> None:
        """Test that unlink is not called if the key file does not exist."""
        key_file = self._make_path(exists=False)
        config_file = self._make_path()
        known_hosts = self._make_path()

        ssh_reconcile_config(
            ssh_key=None,
            key_file=key_file,
            config_file=config_file,
            known_hosts_file=known_hosts,
            known_hosts_content="github.com ssh-rsa AAA...",
            proxy_config=None,
        )

        key_file.unlink.assert_not_called()

    def test_writes_known_hosts(self) -> None:
        """Test that known hosts content is written."""
        key_file = self._make_path(exists=False)
        config_file = self._make_path()
        known_hosts = self._make_path()
        content = "github.com ssh-rsa AAA..."

        ssh_reconcile_config(
            ssh_key=None,
            key_file=key_file,
            config_file=config_file,
            known_hosts_file=known_hosts,
            known_hosts_content=content,
            proxy_config=None,
        )

        known_hosts.write_text.assert_called_once_with(content, mode=0o600)

    def test_config_includes_strict_host_checking(self) -> None:
        """Test that the SSH config includes StrictHostKeyChecking yes."""
        key_file = self._make_path(exists=False)
        config_file = self._make_path()
        known_hosts = self._make_path()

        ssh_reconcile_config(
            ssh_key=None,
            key_file=key_file,
            config_file=config_file,
            known_hosts_file=known_hosts,
            known_hosts_content="",
            proxy_config=None,
        )

        written = config_file.write_text.call_args[0][0]
        assert "StrictHostKeyChecking yes" in written

    def test_config_includes_identity_file_when_key_set(self) -> None:
        """Test that the SSH config includes IdentityFile when a key is provided."""
        key_file = self._make_path(exists=False)
        config_file = self._make_path()
        known_hosts = self._make_path()

        ssh_reconcile_config(
            ssh_key="KEY",
            key_file=key_file,
            config_file=config_file,
            known_hosts_file=known_hosts,
            known_hosts_content="",
            proxy_config=None,
        )

        written = config_file.write_text.call_args[0][0]
        assert "IdentityFile" in written

    def test_config_omits_identity_file_when_no_key(self) -> None:
        """Test that the SSH config omits IdentityFile when no key is provided."""
        key_file = self._make_path(exists=False)
        config_file = self._make_path()
        known_hosts = self._make_path()

        ssh_reconcile_config(
            ssh_key=None,
            key_file=key_file,
            config_file=config_file,
            known_hosts_file=known_hosts,
            known_hosts_content="",
            proxy_config=None,
        )

        written = config_file.write_text.call_args[0][0]
        assert "IdentityFile" not in written

    def test_proxy_command_added(self) -> None:
        """Test that a ProxyCommand is added when proxy config is provided."""
        key_file = self._make_path(exists=False)
        config_file = self._make_path()
        known_hosts = self._make_path()
        proxy = ProxyConfig(
            http_proxy=HttpUrl("http://proxy.example.com:8080"),
            https_proxy=None,
            no_proxy=None,
        )

        ssh_reconcile_config(
            ssh_key=None,
            key_file=key_file,
            config_file=config_file,
            known_hosts_file=known_hosts,
            known_hosts_content="",
            proxy_config=proxy,
        )

        written = config_file.write_text.call_args[0][0]
        assert "ProxyCommand" in written
        assert "proxy.example.com" in written
        assert "8080" in written

    def test_owner_passed_through(self) -> None:
        """Test that owner is passed to mkdir and write_text calls."""
        key_file = self._make_path(exists=False)
        config_file = self._make_path()
        known_hosts = self._make_path()

        ssh_reconcile_config(
            ssh_key="KEY",
            key_file=key_file,
            config_file=config_file,
            known_hosts_file=known_hosts,
            known_hosts_content="hosts",
            proxy_config=None,
            owner="www-data",
        )

        # Key file should be written with user kwarg
        key_file.write_text.assert_called_once_with("KEY\n", mode=0o600, user="www-data")
        # Parent dirs should be created with user kwarg
        key_file.parent.mkdir.assert_called_once_with(
            mode=0o700, parents=True, exist_ok=True, user="www-data"
        )
