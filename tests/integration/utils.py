# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Utility and helper functions for integration tests."""

import time
from collections.abc import Callable, Sequence

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


def req_okay(address: str, timeout: int, verify: bool = False) -> bool:
    response = requests.get(address, timeout=timeout, allow_redirects=True, verify=verify)
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


def any_error_after(
    *, grace: float = 90, fail_fast_apps: Sequence[str] = ()
) -> Callable[[jubilant.Status], bool]:
    """Return a predicate that reports Juju errors persisting past a grace period.

    Errors in fail-fast apps are reported immediately. The default grace period covers
    Juju's first four automatic hook retries, which back off from 5s by a factor of 2.

    Args:
        grace: Seconds an error must persist before the predicate reports it.
        fail_fast_apps: Applications whose errors are reported immediately.

    Returns:
        A predicate that takes a Juju status and returns True if any fail-fast app is in
        error, or if any app or unit has been in error for at least the grace period.
    """
    first_error_at: float | None = None

    def error(status: jubilant.Status) -> bool:
        nonlocal first_error_at
        if fail_fast_apps and jubilant.any_error(status, *fail_fast_apps):
            return True
        if not jubilant.any_error(status):
            first_error_at = None
            return False
        if first_error_at is None:
            first_error_at = time.monotonic()
        return time.monotonic() - first_error_at >= grace

    return error
