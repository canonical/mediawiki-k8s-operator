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
from .utils import juju_exec, req_okay

logger = logging.getLogger(__name__)


@pytest.mark.abort_on_fail
def test_workload_version_is_set(juju: jubilant.Juju, app: App):
    """Check that the charm is the expected version."""
    status = juju.status()
    version = status.apps[app.name].version
    assert "mediawiki" in version.lower(), (
        f"Expected 'mediawiki' in workload version, got {version}"
    )


@pytest.mark.abort_on_fail
def test_tls_certificate_lifecycle(
    juju: jubilant.Juju,
    app: App,
    ssc: App,
    traefik: App,
    ingress_address: str,
    requests_timeout: int,
):
    """Check TLS enablement, HTTP fallback, and re-enablement for later tests."""

    def _tls_material_matches(*, ready: bool) -> bool:
        """Return whether Apache TLS material matches the expected state."""
        predicate = (
            "test -L /etc/apache2/sites-enabled/mediawiki-tls.conf "
            "&& test -s /etc/mediawiki/tls/certificate.pem "
            "&& test -s /etc/mediawiki/tls/private-key.pem"
            if ready
            else "test ! -e /etc/apache2/sites-enabled/mediawiki-tls.conf "
            "&& test ! -e /etc/mediawiki/tls/certificate.pem "
            "&& test ! -e /etc/mediawiki/tls/private-key.pem"
        )
        return juju_exec(juju, app, f"{predicate} && echo matched || true").strip() == "matched"

    juju.integrate(f"{traefik.name}:receive-ca-cert", f"{ssc.name}:send-ca-cert")
    juju.integrate(f"{app.name}:certificates", f"{ssc.name}:certificates")
    juju.wait(
        lambda status: (
            jubilant.all_active(status)
            and "certificates" in status.apps[app.name].relations
            and _tls_material_matches(ready=True)
            and req_okay(ingress_address, requests_timeout)
        ),
        error=jubilant.any_error,
    )

    assert "Syntax OK" in juju_exec(juju, app, "apache2ctl configtest 2>&1")
    assert (
        juju_exec(
            juju,
            app,
            "test -L /etc/apache2/sites-enabled/mediawiki-tls.conf && echo enabled",
        ).strip()
        == "enabled"
    )
    assert (
        juju_exec(
            juju,
            app,
            "test -s /etc/mediawiki/tls/certificate.pem "
            "&& test -s /etc/mediawiki/tls/private-key.pem && echo present",
        ).strip()
        == "present"
    )

    juju.remove_relation(f"{app.name}:certificates", f"{ssc.name}:certificates")
    juju.wait(
        lambda status: (
            jubilant.all_active(status)
            and req_okay(ingress_address, requests_timeout)
            and "certificates" not in status.apps[app.name].relations
            and _tls_material_matches(ready=False)
        ),
        error=jubilant.any_error,
    )
    assert (
        juju_exec(
            juju,
            app,
            "test ! -e /etc/apache2/sites-enabled/mediawiki-tls.conf "
            "&& test ! -e /etc/mediawiki/tls/certificate.pem "
            "&& test ! -e /etc/mediawiki/tls/private-key.pem && echo removed",
        ).strip()
        == "removed"
    )

    juju.integrate(f"{app.name}:certificates", f"{ssc.name}:certificates")
    juju.wait(
        lambda status: (
            jubilant.all_active(status)
            and "certificates" in status.apps[app.name].relations
            and _tls_material_matches(ready=True)
            and req_okay(ingress_address, requests_timeout)
        ),
        error=jubilant.any_error,
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
    app_config["local-settings"] += "wfLoadExtension( 'CheckUser' );\n"
    app_config["local-settings"] += "wfLoadExtension( 'Linter' );\n"
    app_config["local-settings"] += "wfLoadExtension( 'DiscussionTools' );\n"
    app_config["local-settings"] += "wfLoadExtension( 'Echo' );\n"
    app_config["local-settings"] += "wfLoadExtension( 'Thanks' );\n"
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
def test_create_and_promote_action(juju: jubilant.Juju, app: App):
    """Check that the create-and-promote action works as expected."""
    create_action = juju.run(
        f"{app.name}/leader",
        "create-and-promote",
        {"username": "alice", "bureaucrat": True, "generate-password": True},
    )
    assert create_action.status == "completed"
    assert create_action.results["username"] == "alice"
    assert len(create_action.results["password"]) >= 64, (
        "Expected generated password to be at least 64 characters long"
    )  # secrets.token_urlsafe(64) averages 1.3 characters per byte to 83

    # Re-running without force should fail because the user already exists, even
    # when a password would be generated.
    with pytest.raises(jubilant.TaskError):
        juju.run(
            f"{app.name}/leader",
            "create-and-promote",
            {"username": "alice", "bureaucrat": True, "generate-password": True},
        )

    # Creating a user without generating a password and without force should fail
    # validation.
    with pytest.raises(jubilant.TaskError):
        juju.run(
            f"{app.name}/leader",
            "create-and-promote",
            {"username": "alice", "generate-password": False},
        )

    # Promoting an existing user with force and no password generation should
    # succeed and not return a password.
    promote_action = juju.run(
        f"{app.name}/leader",
        "create-and-promote",
        {"username": "alice", "generate-password": False, "force": True, "sysop": True},
    )
    assert promote_action.status == "completed"
    assert promote_action.results["username"] == "alice"
    assert "password" not in promote_action.results, (
        "Did not expect a password to be returned when generation is disabled"
    )

    juju.wait(jubilant.all_active)


@pytest.mark.abort_on_fail
def test_force_reconciliation_action(juju: jubilant.Juju, app: App):
    """Check that the force-reconciliation action works on the leader and on all units."""
    # Single unit (leader)
    action = juju.run(f"{app.name}/leader", "force-reconciliation")
    assert action.status == "completed", f"force-reconciliation on leader failed: {action.message}"
    juju.wait(jubilant.all_active)

    # All units via peer coordination
    all_units_action = juju.run(f"{app.name}/leader", "force-reconciliation", {"all-units": True})
    assert all_units_action.status == "completed", (
        f"force-reconciliation all-units failed: {all_units_action.message}"
    )
    # The update completes asynchronously via peer relation coordination
    juju.wait(jubilant.all_active, successes=5)


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
    juju.wait(
        lambda status: (
            jubilant.all_active(status)
            and not is_reachable()
            and "traefik-route" not in status.apps[app.name].relations
        )
    )

    juju.integrate(app.name, traefik.name)
    juju.wait(
        lambda status: (
            jubilant.all_active(status)
            and is_reachable()
            and "traefik-route" in status.apps[app.name].relations
        )
    )

    # Removing database blocks and stops responsiveness entirely
    juju.remove_relation(app.name, db.name)
    juju.wait(lambda status: status.apps[app.name].is_blocked and not is_reachable())
    juju.wait(
        lambda status: (
            jubilant.all_active(status, db.name)
            and "database" not in status.apps[app.name].relations
        )
    )

    juju.integrate(app.name, db.name)
    juju.wait(
        lambda status: (
            jubilant.all_active(status)
            and is_reachable()
            and "database" in status.apps[app.name].relations
        )
    )

    # Removing Redis does not block
    juju.remove_relation(app.name, redis.name)
    juju.wait(
        lambda status: (
            jubilant.all_active(status)
            and is_reachable()
            and "redis" not in status.apps[app.name].relations
        ),
        successes=5,
    )

    juju.integrate(app.name, redis.name)
    juju.wait(
        lambda status: (
            jubilant.all_active(status)
            and is_reachable()
            and "redis" in status.apps[app.name].relations
        ),
        successes=5,
    )
