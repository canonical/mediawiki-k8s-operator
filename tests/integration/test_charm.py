#!/usr/bin/env python3

# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""

import functools
import logging
from pathlib import Path
from typing import Any

import jubilant
import requests

from .types_ import App

logger = logging.getLogger(__name__)


def test_active(juju: jubilant.Juju, app: App, ingress_address: str, requests_timeout: int):
    """Check that the charm is active.
    Assume that the charm has already been built and is running.
    """
    status = juju.status()
    assert status.apps[app.name].units[app.name + "/0"].is_active

    assert req_okay(ingress_address, requests_timeout), (
        f"MediaWiki not responding at ingress {ingress_address}"
    )


def test_workload_version_is_set(juju: jubilant.Juju, app: App):
    """Check that the charm is the expected version."""
    status = juju.status()
    version = status.apps[app.name].version
    assert "mediawiki" in version.lower(), (
        f"Expected 'mediawiki' in workload version, got {version}"
    )


def test_add_extensions(
    juju: jubilant.Juju,
    app: App,
    app_config: dict[str, Any],
    ingress_address: str,
    requests_timeout: int,
):
    """Check that the charm can have extensions added after deployment.

    Add extensions by editing the composer config, and then running a database update.
    """
    is_reachable = functools.partial(req_okay, address=ingress_address, timeout=requests_timeout)

    juju.wait(jubilant.all_active)
    assert is_reachable(), "MediaWiki not responding at ingress before adding extensions"

    composer = Path(__file__).parent / "test_data" / "composer.json"
    app_config["composer"] = composer.read_text()
    app_config["local-settings"] += "wfLoadExtension( 'UserMerge' );\n"
    app_config["local-settings"] += "wfLoadExtension( 'PageTriage' );\n"
    app_config["local-settings"] += "wfLoadExtension( 'Mermaid' );\n"

    juju.config(app.name, app_config)
    juju.wait(jubilant.all_active)

    update_database_action = juju.run(f"{app.name}/leader", "update-database")
    assert update_database_action.status == "completed"

    assert is_reachable(), "MediaWiki not responding at ingress after adding extensions"

    loaded_extensions = requests.get(
        f"{ingress_address}/w/api.php?action=query&meta=siteinfo&siprop=extensions&format=json&formatversion=2",
        timeout=requests_timeout,
    ).json()

    loaded_extensions = {ext["name"] for ext in loaded_extensions["query"]["extensions"]}
    assert "UserMerge" in loaded_extensions, "UserMerge extension not loaded"
    assert "PageTriage" in loaded_extensions, "PageTriage extension not loaded"
    assert "Mermaid" in loaded_extensions, "Mermaid extension not loaded"


def test_rotate_root_credentials_action(juju: jubilant.Juju, app: App):
    """Check that the rotate-root-credentials action works as expected."""
    juju.wait(jubilant.all_active)

    rotate_action = juju.run(f"{app.name}/leader", "rotate-root-credentials")
    assert rotate_action.status == "completed"
    assert rotate_action.results["username"] == "root"
    assert len(rotate_action.results["password"]) >= 64, (
        "Expected new password to be at least 64 characters long)"
    )  # secrets.url_base(64) averages 1.3 characters per byte to 83

    new_rotate_action = juju.run(f"{app.name}/leader", "rotate-root-credentials")
    assert new_rotate_action.status == "completed"
    assert rotate_action.results["password"] != new_rotate_action.results["password"], (
        "Expected new password to be different from previous password"
    )


def test_relations(
    juju: jubilant.Juju,
    app: App,
    db: App,
    traefik: App,
    ingress_address: str,
    requests_timeout: int,
):
    """Check that the charm behaves correctly when certain relations are removed."""
    is_reachable = functools.partial(req_okay, address=ingress_address, timeout=requests_timeout)

    juju.wait(jubilant.all_active)
    assert is_reachable(), (
        f"MediaWiki not responding at {ingress_address} before removing relations"
    )

    # Remove traefik relation and check that the charm remains active, but the ingress address is no longer responsive, but unit address is
    juju.remove_relation(app.name, traefik.name)
    juju.wait(lambda status: jubilant.all_active(status) and not is_reachable())

    juju.integrate(app.name, traefik.name)
    juju.wait(lambda status: jubilant.all_active(status) and is_reachable())

    # Removing database blocks and makes stops responsiveness entirely
    juju.remove_relation(app.name, db.name)
    juju.wait(lambda status: status.apps[app.name].is_blocked and not is_reachable())

    juju.integrate(app.name, db.name)
    juju.wait(lambda status: jubilant.all_active(status) and is_reachable())


def test_scaled_db(juju: jubilant.Juju, db: App, ingress_address: str, requests_timeout: int):
    """Check MediaWiki is reachable after scaling the database up."""
    is_reachable = functools.partial(req_okay, address=ingress_address, timeout=requests_timeout)

    juju.wait(jubilant.all_active)
    assert is_reachable(), "MediaWiki not responding at ingress before scaling db"

    juju.add_unit(db.name)
    juju.wait(lambda status: jubilant.all_active(status) and is_reachable())


def req_okay(address: str, timeout: int) -> bool:
    response = requests.get(address, timeout=timeout, allow_redirects=True)
    return response.status_code == 200
