# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Utility functions for the MediaWiki charm."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from charmlibs.pathops import ContainerPath

    from state import ProxyConfig

logger = logging.getLogger(__name__)


def escape_php_string(s: str) -> str:
    """Escape PHP special characters in string."""
    return s.replace("\\", "\\\\").replace("'", "\\'")


def ssh_reconcile_config(
    *,
    ssh_key: str | None,
    key_file: ContainerPath,
    config_file: ContainerPath,
    known_hosts_file: ContainerPath,
    known_hosts_content: str,
    proxy_config: ProxyConfig | None,
    owner: str | None = None,
) -> None:
    """Write or remove an SSH private key and generate an SSH config file.

    Args:
        ssh_key: PEM-encoded private key content, or None to remove any
            existing key.
        key_file: Path inside the container where the private key is stored.
        config_file: Path inside the container for the SSH config file.
        known_hosts_file: Path inside the container for the SSH known hosts file.
        known_hosts_content: The content to write to the known hosts file.
        proxy_config: Optional proxy configuration; when present and
            ``http_proxy`` is set, a ``ProxyCommand`` directive is added.
        owner: Optional OS user that should own the written files.  When
            ``None``, ownership is left to the container default (root).
    """
    ownership = {"user": owner} if owner else {}

    if ssh_key:
        ssh_key = ssh_key.strip() + "\n"
        key_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True, **ownership)
        key_file.write_text(ssh_key, mode=0o600, **ownership)
        logger.debug("SSH key written to %s.", key_file)
    elif key_file.exists():
        key_file.unlink()
        logger.debug("SSH key removed from %s.", key_file)

    known_hosts_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True, **ownership)
    known_hosts_file.write_text(known_hosts_content, mode=0o600, **ownership)
    logger.debug("SSH known hosts written to %s.", known_hosts_file)

    ssh_config_lines = [
        "Host *",
        "    StrictHostKeyChecking yes",
        f"    UserKnownHostsFile {known_hosts_file}",
    ]
    if ssh_key:
        ssh_config_lines.append(f"    IdentityFile {key_file}")

    if proxy_config and proxy_config.http_proxy:
        proxy_host = str(proxy_config.http_proxy.host)
        if not proxy_config.http_proxy.port:
            logger.debug(
                "Using fallback proxy port 3128 for SSH ProxyCommand "
                "because proxy configuration did not include a port."
            )
        proxy_port = str(proxy_config.http_proxy.port) if proxy_config.http_proxy.port else "3128"
        ssh_config_lines.append(
            f"    ProxyCommand socat - PROXY:{proxy_host}:%h:%p,proxyport={proxy_port}"
        )
    ssh_config = "\n".join(ssh_config_lines) + "\n"

    config_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True, **ownership)
    config_file.write_text(ssh_config, mode=0o600, **ownership)
    logger.debug("SSH configuration written to %s.", config_file)


def extract_remote(repo_url: str) -> str | None:
    """Extract the SSH remote hostname from a git repo URL.

    Supports:
      - git@<remote>:<user>/...
      - ssh://<user>@<remote>/...
      - git+ssh://<user>@<remote>/...

    Returns the hostname, or None if no SSH remote could be parsed.
    """
    if repo_url.startswith(("ssh://", "git+ssh://")):
        # URL-style SSH: <scheme>://<user>@<remote>/...
        parsed = urlparse(repo_url)
        return parsed.hostname

    if not repo_url.startswith(("http://", "https://")):
        # SCP-style: git@<remote>:<user>/...
        matches = re.findall(r"@(.+?):", repo_url)
        return matches[0] if matches else None

    return None


def remote_in_known_hosts(remote: str, known_hosts_content: str) -> bool:
    """Check whether *remote* appears as a host entry in *known_hosts_content*."""
    for line in known_hosts_content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        fields = stripped.split()
        if not fields:
            continue

        # Revoked entries should never be treated as valid host matches.
        if fields[0] == "@revoked":
            continue

        host_field_index = 1 if fields[0].startswith("@") else 0
        if len(fields) <= host_field_index:
            continue

        for host_entry in fields[host_field_index].split(","):
            if host_entry == remote:
                return True

            bracketed_match = re.fullmatch(r"\[(.+)\]:(\d+)", host_entry)
            if bracketed_match and bracketed_match.group(1) == remote:
                return True
    return False
