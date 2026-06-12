#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""MediaWiki SAML integration tests."""

import logging
import textwrap
from typing import Generator

import jubilant
import pytest
import requests
import urllib3.exceptions
from saml_test_helper import SamlK8sTestHelper

from .types_ import App

logger = logging.getLogger(__name__)

# PluggableAuth attribute mapping for the crewjam/saml test IdP.
# The IdP sends attributes using OID format:
#   urn:oid:0.9.2342.19200300.100.1.1 (uid)   -> UserName
#   urn:oid:1.3.6.1.4.1.5923.1.1.1.6(eduPersonPrincipalName)   -> UserEmail
SAML_PLUGGABLE_AUTH_LOCAL_SETTINGS = textwrap.dedent("""\
    $wgPluggableAuth_Config['SimpleSAMLphp'] = [
        'data' => [
            'usernameAttribute' => 'urn:oid:0.9.2342.19200300.100.1.1',
            'realNameAttribute' => 'urn:oid:0.9.2342.19200300.100.1.1',
            'emailAttribute' => 'urn:oid:1.3.6.1.4.1.5923.1.1.1.6',
        ],
    ];
    """)


@pytest.fixture(scope="module")
def setup_saml_config(
    juju: jubilant.Juju, app: App, local_settings: str
) -> Generator[SamlK8sTestHelper, None, None]:
    """Deploy SAML test IdP and configure saml-integrator for MediaWiki.

    This uses the saml-test-helper library to deploy a test IdP,
    configure trust on the relevant pods, and integrate with saml-integrator.
    """
    assert juju.model is not None, "juju.model must be set for SAML test deployment"
    model = juju.model

    saml_helper = SamlK8sTestHelper.deploy_saml_idp(model)
    saml_integrator = "saml-integrator"

    juju.deploy(
        saml_integrator,
        channel="latest/edge",
    )

    juju.wait(jubilant.all_agents_idle, timeout=10 * 60)

    # Prepare pods to trust the test IdP's TLS certificates
    saml_helper.prepare_pod(model, f"{saml_integrator}-0")
    for unit_name in juju.status().apps[app.name].units:
        pod_name = unit_name.replace("/", "-")
        saml_helper.prepare_pod(model, pod_name)

    juju.wait(jubilant.all_agents_idle, timeout=10 * 60)

    juju.config(
        saml_integrator,
        {
            "entity_id": saml_helper.entity_id,
            "metadata_url": saml_helper.metadata_url,
        },
    )

    # Configure MediaWiki with SAML attribute mappings via local-settings
    saml_local_settings = local_settings + "\n" + SAML_PLUGGABLE_AUTH_LOCAL_SETTINGS
    juju.config(
        app.name,
        {"local-settings": saml_local_settings},
    )

    juju.integrate(app.name, saml_integrator)
    juju.wait(jubilant.all_agents_idle, timeout=5 * 60)

    yield saml_helper


@pytest.mark.abort_on_fail
def test_saml_login(
    juju: jubilant.Juju,
    requests_timeout: int,
    setup_saml_config: SamlK8sTestHelper,
    ingress_address: str,
):
    """Test that a user can log in to MediaWiki using SAML authentication.

    arrange: MediaWiki is deployed with SAML configured and a test IdP running.
    act: Register a user in the IdP, initiate SAML login via MediaWiki.
    assert: The user is authenticated and can access their user page.
    """
    saml_helper = setup_saml_config
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    username = "testuser"
    # MW canonicalizes usernames with uppercase first letter
    canonical_username = username[0].upper() + username[1:]
    email = "testuser@canonical.com"
    password = "test-mediawiki-k8s-password"  # nosec B105
    saml_helper.register_user(username=username, email=email, password=password)

    session = requests.Session()
    session.verify = False

    # Fetch SP metadata and register with the IdP
    juju.wait(jubilant.all_active)
    sp_metadata_url = f"{ingress_address}/w/simplesaml/module.php/saml/sp/metadata.php/default-sp"
    sp_metadata_response = session.get(sp_metadata_url, timeout=requests_timeout)
    assert sp_metadata_response.status_code == 200, (
        f"Failed to fetch SP metadata: {sp_metadata_response.status_code}"
    )
    saml_helper.register_service_provider(name="mediawiki-k8s", metadata=sp_metadata_response.text)

    # Verify the user page doesn't exist before login
    user_page_url = f"{ingress_address}/w/index.php?title=User:{canonical_username}"
    user_page_response = session.get(user_page_url, timeout=requests_timeout)
    assert user_page_response.status_code == 404, (
        f"User page for '{canonical_username}' already exists before SAML login. "
        f"Status code: {user_page_response.status_code}"
    )

    # Initiate SAML login by visiting the login page.
    # PluggableAuth auto-redirects through the AuthManager flow to the IdP.
    # We follow redirects manually to capture the IdP URL without resolving
    # saml.canonical.test (which isn't in the test runner's DNS).
    redirect_url: str = f"{ingress_address}/w/index.php?title=Special:UserLogin&returnto=Main+Page"
    response = session.get(redirect_url, timeout=requests_timeout, allow_redirects=False)
    redirect_url = response.headers.get("Location", "")
    max_redirects = 20
    redirects_followed = 0
    redirect_history: list[tuple[str, requests.Response]] = [(redirect_url, response)]
    while redirect_url and "saml.canonical.test" not in redirect_url:
        redirects_followed += 1
        assert redirects_followed <= max_redirects, (
            f"Exceeded maximum number of redirects ({max_redirects}). "
            f"Last redirect URL: {redirect_url}, status: {response.status_code}"
        )
        logger.info("Following redirect: %s", redirect_url)
        if redirect_url.startswith("/"):
            redirect_url = f"{ingress_address}{redirect_url}"
        response = session.get(redirect_url, timeout=requests_timeout, allow_redirects=False)
        redirect_url = response.headers.get("Location", "")
        redirect_history.append((redirect_url, response))

    if "saml.canonical.test" not in redirect_url:
        history_dump = "\n".join(
            f"  [{i}] {url} ({resp.status_code}):\n{resp.text}"
            for i, (url, resp) in enumerate(redirect_history)
        )
        raise AssertionError(
            f"Expected redirect to IdP (saml.canonical.test), but got: {redirect_url}. "
            f"Final response status: {response.status_code}\n"
            f"Redirect history:\n{history_dump}"
        )

    # Complete the SSO login on the IdP side
    saml_response = saml_helper.redirect_sso_login(
        redirect_url, username=username, password=password
    )
    logger.info("ACS URL: %s", saml_response.url)

    # Post the SAML response back to MediaWiki's assertion consumer service.
    # Follow redirects manually to handle the auth completion flow.
    response = session.post(
        saml_response.url,
        data={"SAMLResponse": saml_response.data["SAMLResponse"], "SameSite": "1"},
        timeout=requests_timeout,
    )
    assert response.status_code == 200, (
        f"ACS POST failed with status code {response.status_code} and response: {response.text}"
    )

    # Verify the user profile page was created
    user_page_url = f"{ingress_address}/w/index.php?title=User:{canonical_username}"
    user_page_response = session.get(user_page_url, timeout=requests_timeout)
    assert user_page_response.status_code == 200, (
        f"Failed to access user page after SAML login: {user_page_response.status_code}"
    )
    assert f"User:{canonical_username}" in user_page_response.text, (
        f"User page for '{canonical_username}' not found after SAML login. "
    )

    # Verify session
    response = session.get(
        f"{ingress_address}/w/api.php",
        params={
            "action": "query",
            "meta": "userinfo",
            "format": "json",
        },
    )
    assert response.status_code == 200, (
        f"Failed to query user info after SAML login: {response.status_code}"
    )
    assert response.json()["query"]["userinfo"]["name"] == canonical_username, (
        f"Expected username '{canonical_username}' in user info, but got: {response.json()['query']['userinfo']['name']}"
    )


@pytest.mark.abort_on_fail
def test_saml_requires_redis(app: App, juju: jubilant.Juju, redis: App):
    """Test that removing Redis while SAML is integrated causes blocked status.

    arrange: MediaWiki is deployed with SAML and Redis integrated.
    act: Remove the Redis relation.
    assert: The charm enters blocked status. Re-adding Redis restores active.
    """
    # Remove Redis relation; the charm should enter blocked status
    juju.remove_relation(app.name, redis.name)
    juju.wait(
        lambda status: (
            jubilant.all_blocked(status, app.name)
            and "redis" not in status.apps[app.name].relations
        ),
    )

    # Re-add Redis; the charm should return to active
    juju.integrate(app.name, redis.name)
    juju.wait(
        lambda status: jubilant.all_active(status, app.name),
    )


@pytest.mark.abort_on_fail
def test_saml_removal(app: App, juju: jubilant.Juju):
    """Test that removing the SAML integration returns MediaWiki to active.

    arrange: MediaWiki is deployed with SAML integrated.
    act: Remove the saml-integrator relation.
    assert: MediaWiki returns to active status without SAML.
    """
    juju.remove_relation(app.name, "saml-integrator")
    juju.wait(
        lambda status: (
            jubilant.all_active(status, app.name) and "saml" not in status.apps[app.name].relations
        ),
    )
