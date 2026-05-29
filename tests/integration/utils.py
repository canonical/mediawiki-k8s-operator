# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Utility and helper functions for integration tests."""

import jubilant
import requests

from .types_ import App


def kubectl(namespace: str | None, *args: str) -> list[str]:
    """Build a kubectl command, scoping to *namespace* when provided."""
    cmd = ["kubectl"]
    if namespace:
        cmd.extend(["-n", namespace])
    cmd.extend(args)
    return cmd


def req_okay(address: str, timeout: int) -> bool:
    response = requests.get(address, timeout=timeout, allow_redirects=True)
    return response.status_code == 200


def juju_exec(
    juju: jubilant.Juju,
    app: App,
    cmd: str,
    *,
    unit: str | int = "leader",
    container: str = "mediawiki",
) -> str:
    """Execute a command in a container of a target unit for *app*."""
    if unit == "leader":
        target = f"{app.name}/leader"
    elif "/" in str(unit):
        target = str(unit)
    else:
        target = f"{app.name}/{unit}"

    return juju.ssh(target, cmd, container=container)
