#!/usr/bin/env python3

# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""

import functools
import logging
from pathlib import Path
from typing import Any

import jubilant
import pytest
import requests

from .types_ import App
from .utils import req_okay

logger = logging.getLogger(__name__)


@pytest.mark.abort_on_fail
def test_active(juju: jubilant.Juju, app: App, ingress_address: str, requests_timeout: int):
    """Check that the charm is active.
    Assume that the charm has already been built and is running.
    """
    status = juju.status()
    assert status.apps[app.name].units[app.name + "/0"].is_active

    assert req_okay(ingress_address, requests_timeout), (
        f"MediaWiki not responding at ingress {ingress_address}"
    )


@pytest.mark.abort_on_fail
def test_workload_version_is_set(juju: jubilant.Juju, app: App):
    """Check that the charm is the expected version."""
    status = juju.status()
    version = status.apps[app.name].version
    assert "mediawiki" in version.lower(), (
        f"Expected 'mediawiki' in workload version, got {version}"
    )


@pytest.mark.abort_on_fail
def test_ssh_key_secret(
    juju: jubilant.Juju, app: App, app_config: dict[str, Any], ssh_key_secret: str
):
    """Check that the charm behaves correct regarding the ssh_key Juju secret.

    Note that this test does not attempt to utilize the SSH key as the passed key is not expected to be
    authorized anywhere.
    """
    initial_secret_content = juju.show_secret(ssh_key_secret, reveal=True).content

    app_config["ssh-key"] = ssh_key_secret
    juju.config(app.name, app_config)
    juju.wait(jubilant.all_active, successes=5)

    # Block due to empty mediawiki SSH key
    juju.update_secret(ssh_key_secret, {"mediawiki": ""})
    juju.wait(lambda status: jubilant.all_blocked(status, app.name))

    # Reset secret
    juju.update_secret(ssh_key_secret, initial_secret_content)
    juju.wait(jubilant.all_active)

    # Block due to no valid keys in secret
    juju.update_secret(ssh_key_secret, {"invalid-field": "value"})
    juju.wait(lambda status: jubilant.all_blocked(status, app.name))

    app_config.pop("ssh-key")
    juju.config(app.name, app_config, reset="ssh-key")
    juju.wait(jubilant.all_active)


@pytest.mark.abort_on_fail
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
    # The DB update completes asynchronously to the action, so we need to be certain that no other actions are still running
    juju.wait(jubilant.all_active, successes=5)

    assert is_reachable(), "MediaWiki not responding at ingress after adding extensions"

    loaded_extensions = requests.get(
        f"{ingress_address}/w/api.php?action=query&meta=siteinfo&siprop=extensions&format=json&formatversion=2",
        timeout=requests_timeout,
    ).json()

    loaded_extensions = {ext["name"] for ext in loaded_extensions["query"]["extensions"]}
    assert "UserMerge" in loaded_extensions, "UserMerge extension not loaded"
    assert "PageTriage" in loaded_extensions, "PageTriage extension not loaded"
    assert "Mermaid" in loaded_extensions, "Mermaid extension not loaded"


@pytest.mark.abort_on_fail
def test_rotate_mediawiki_secrets_action(juju: jubilant.Juju, app: App):
    """Check that the rotate-mediawiki-secrets action works as expected."""
    rotate_action = juju.run(f"{app.name}/leader", "rotate-mediawiki-secrets")
    assert rotate_action.status == "completed", (
        f"Action failed with message: {rotate_action.message}"
    )

    juju.wait(jubilant.all_active)


@pytest.mark.abort_on_fail
def test_rotate_root_credentials_action(juju: jubilant.Juju, app: App):
    """Check that the rotate-root-credentials action works as expected."""
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

    juju.wait(jubilant.all_active)


@pytest.mark.abort_on_fail
def test_upload(
    juju: jubilant.Juju,
    app: App,
    ingress_address: str,
    requests_timeout: int,
):
    """Check uploading a file to MediaWiki via the API."""
    juju.wait(jubilant.all_active)

    rotate_action = juju.run(f"{app.name}/leader", "rotate-root-credentials")
    assert rotate_action.status == "completed"
    username = rotate_action.results["username"]
    password = rotate_action.results["password"]

    url = f"{ingress_address}/w/api.php"
    with requests.Session() as session:
        # Retrieve a login token
        req = session.get(
            url=url,
            params={
                "action": "query",
                "meta": "tokens",
                "type": "login",
                "format": "json",
            },
            timeout=requests_timeout,
        )
        login_token = req.json()["query"]["tokens"]["logintoken"]

        # Log in
        req = session.post(
            url=url,
            data={
                "action": "login",
                "lgname": username,
                "lgpassword": password,
                "lgtoken": login_token,
                "format": "json",
            },
            timeout=requests_timeout,
        )
        assert req.status_code == 200, f"Expected status code 200, got {req.status_code}"

        # Get CSRF token
        req = session.get(
            url=url,
            params={
                "action": "query",
                "meta": "tokens",
                "format": "json",
            },
            timeout=requests_timeout,
        )
        csrf_token = req.json()["query"]["tokens"]["csrftoken"]

        # Upload
        with open(Path(__file__).parent / "test_data" / "test_image.png", "rb") as f:
            image_data = f.read()

        req = session.post(
            url=url,
            data={
                "action": "upload",
                "filename": "Test-Image.png",
                "token": csrf_token,
                "format": "json",
                "ignorewarnings": 1,
            },
            files={"file": ("Test-Image.png", image_data, "multipart/form-data")},
            timeout=requests_timeout,
        )

    logger.info("Upload response: %s", req.text)
    assert req.status_code == 200, f"Expected status code 200, got {req.status_code}"
    assert "upload" in req.json(), f"Expected 'upload' in response, got {req.json()}"
    assert req.json()["upload"]["result"] == "Success", (
        f"Expected upload result to be 'Success', got {req.json()['upload']['result']}"
    )


@pytest.mark.abort_on_fail
def test_relations(
    juju: jubilant.Juju,
    app: App,
    db: App,
    traefik: App,
    redis: App,
    ingress_address: str,
    requests_timeout: int,
):
    """Check that the charm behaves correctly when certain relations are removed."""
    is_reachable = functools.partial(req_okay, address=ingress_address, timeout=requests_timeout)

    juju.wait(jubilant.all_active)
    assert is_reachable(), (
        f"MediaWiki not responding at {ingress_address} before removing relations"
    )

    # Remove traefik relation and check that the charm remains active, but the ingress address is no longer responsive
    juju.remove_relation(app.name, traefik.name)
    juju.wait(lambda status: jubilant.all_active(status) and not is_reachable())

    juju.integrate(app.name, traefik.name)
    juju.wait(lambda status: jubilant.all_active(status) and is_reachable())

    # Removing database blocks and stops responsiveness entirely
    juju.remove_relation(app.name, db.name)
    juju.wait(lambda status: status.apps[app.name].is_blocked and not is_reachable())
    juju.wait(lambda status: jubilant.all_active(status, db.name))

    juju.integrate(app.name, db.name)
    juju.wait(lambda status: jubilant.all_active(status) and is_reachable())

    # Removing Redis does not block
    juju.remove_relation(app.name, redis.name)
    juju.wait(lambda status: jubilant.all_active(status) and is_reachable(), successes=5)

    juju.integrate(app.name, redis.name)
    juju.wait(lambda status: jubilant.all_active(status) and is_reachable(), successes=5)
