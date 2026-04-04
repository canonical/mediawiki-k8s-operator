# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Utility and helper functions for integration tests."""

import requests


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
