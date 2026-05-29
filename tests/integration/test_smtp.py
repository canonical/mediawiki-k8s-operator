# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for SMTP."""

import json
import logging
import subprocess  # nosec: B404
import textwrap
from typing import Generator

import jubilant
import pytest

from .types_ import App
from .utils import kubectl

logger = logging.getLogger(__name__)

_MAILPIT_SMTP_PORT = 1025
_MAILPIT_HTTP_PORT = 8025
_SMTP_USER = "mediawiki"
_SMTP_PASSWORD = "smtp-test-password"  # nosec: B105
_SMTP_SENDER = "mediawiki@example.com"


def _kubectl_run(namespace: str | None, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a kubectl command, logging stderr on failure."""
    result = subprocess.run(  # nosec: B603
        kubectl(namespace, *args),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        logger.error("kubectl failed: %s\nstderr: %s", args, result.stderr)
        result.check_returncode()
    return result


def _exec_in_mediawiki(juju: jubilant.Juju, app: App, cmd: str) -> str:
    """Execute a command in the mediawiki container of the leader unit."""
    return juju.ssh(f"{app.name}/0", cmd, container="mediawiki")


def _query_mailpit(namespace: str, endpoint: str) -> dict:
    """Query the mailpit HTTP API via kubectl exec."""
    result = _kubectl_run(
        namespace,
        "exec",
        "pod/mailpit",
        "--",
        "wget",
        "--header=Accept: application/json",
        "-q",
        "-O-",
        f"http://localhost:{_MAILPIT_HTTP_PORT}{endpoint}",
    )
    return json.loads(result.stdout)


@pytest.fixture(scope="module", name="namespace")
def namespace_fixture(juju: jubilant.Juju) -> str:
    """Return the Juju model namespace (needed for kubectl commands)."""
    assert juju.model is not None, "Juju model must be set"
    return juju.model


@pytest.fixture(scope="module", name="mailpit")
def mailpit_fixture(namespace: str) -> Generator[str, None, None]:
    """Deploy mailpit as a pod+service in the model namespace with STARTTLS and auth.

    Yields the in-cluster service hostname (e.g. mailpit.<ns>.svc.cluster.local).
    """
    # Create a pod with an init container that generates a self-signed TLS cert,
    # then runs mailpit with STARTTLS required and any-auth accepted.
    pod_manifest = textwrap.dedent(f"""\
        apiVersion: v1
        kind: Pod
        metadata:
          name: mailpit
          labels:
            app: mailpit
        spec:
          initContainers:
          - name: cert-gen
            image: docker.io/ubuntu:latest
            command:
            - sh
            - -c
            - |
              apt-get update -qq && apt-get install -y -qq openssl && \\
              openssl req -x509 -newkey rsa:2048 -keyout /certs/tls.key \\
                -out /certs/tls.crt -days 1 -nodes -subj "/CN=mailpit"
            volumeMounts:
            - name: certs
              mountPath: /certs
          containers:
          - name: mailpit
            image: docker.io/axllent/mailpit:latest
            args:
            - --smtp-tls-cert=/certs/tls.crt
            - --smtp-tls-key=/certs/tls.key
            - --smtp-require-tls
            - --smtp-auth-accept-any
            ports:
            - containerPort: {_MAILPIT_SMTP_PORT}
              name: smtp
            - containerPort: {_MAILPIT_HTTP_PORT}
              name: http
            volumeMounts:
            - name: certs
              mountPath: /certs
          volumes:
          - name: certs
            emptyDir: {{}}
    """)

    svc_manifest = textwrap.dedent(f"""\
        apiVersion: v1
        kind: Service
        metadata:
          name: mailpit
        spec:
          selector:
            app: mailpit
          ports:
          - name: smtp
            port: {_MAILPIT_SMTP_PORT}
            targetPort: {_MAILPIT_SMTP_PORT}
          - name: http
            port: {_MAILPIT_HTTP_PORT}
            targetPort: {_MAILPIT_HTTP_PORT}
    """)

    # Apply using subprocess directly to pass stdin
    subprocess.run(  # nosec: B603
        kubectl(namespace, "apply", "-f", "-"),
        input=pod_manifest,
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(  # nosec: B603
        kubectl(namespace, "apply", "-f", "-"),
        input=svc_manifest,
        capture_output=True,
        text=True,
        check=True,
    )

    # Wait for pod to be ready
    _kubectl_run(namespace, "wait", "--for=condition=Ready", "pod/mailpit", "--timeout=120s")

    hostname = f"mailpit.{namespace}.svc.cluster.local"
    yield hostname

    # Cleanup
    _kubectl_run(namespace, "delete", "pod/mailpit", "--ignore-not-found")
    _kubectl_run(namespace, "delete", "service/mailpit", "--ignore-not-found")


@pytest.fixture(scope="module", name="smtp_integrator")
def smtp_integrator_fixture(
    juju: jubilant.Juju,
    mailpit: str,
    pytestconfig: pytest.Config,
) -> Generator[App, None, None]:
    """Deploy smtp-integrator configured for mailpit with STARTTLS + auth."""
    use_existing = pytestconfig.getoption("--use-existing", default=False)
    if use_existing:
        yield App(name="smtp-integrator")
        return

    secret_uri = juju.add_secret(
        "smtp-credentials",
        {"password": _SMTP_PASSWORD},
    )

    juju.deploy(
        "smtp-integrator",
        channel="latest/stable",
        config={
            "host": mailpit,
            "port": str(_MAILPIT_SMTP_PORT),
            "transport_security": "tls",
            "auth_type": "plain",
            "user": _SMTP_USER,
            "password": secret_uri,
            "skip_ssl_verify": "true",
            "smtp_sender": _SMTP_SENDER,
        },
    )

    juju.grant_secret(secret_uri, "smtp-integrator")
    juju.wait(lambda status: jubilant.all_active(status, "smtp-integrator"))

    yield App(name="smtp-integrator")


@pytest.fixture(scope="module", name="root_credentials")
def root_credentials_fixture(juju: jubilant.Juju, app: App) -> tuple[str, str]:
    """Rotate and return the root bureaucrat credentials once per module."""
    rotate_action = juju.run(f"{app.name}/leader", "rotate-root-credentials")
    assert rotate_action.status == "completed"
    return rotate_action.results["username"], rotate_action.results["password"]


@pytest.fixture(scope="module", name="authenticated_session")
def authenticated_session_fixture(
    requests_timeout: int,
    ingress_address: str,
    root_credentials: tuple[str, str],
) -> Generator[tuple, None, None]:
    """Return an authenticated MediaWiki session with a CSRF token.

    Yields (session, csrf_token, api_url).
    """
    import requests

    username, password = root_credentials
    url = f"{ingress_address}/w/api.php"
    session = requests.Session()

    # Get login token
    resp = session.get(
        url=url,
        params={"action": "query", "meta": "tokens", "type": "login", "format": "json"},
        timeout=requests_timeout,
    )
    login_token = resp.json()["query"]["tokens"]["logintoken"]

    # Log in
    resp = session.post(
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
    assert resp.status_code == 200

    # Get CSRF token
    resp = session.get(
        url=url,
        params={"action": "query", "meta": "tokens", "format": "json"},
        timeout=requests_timeout,
    )
    csrf_token = resp.json()["query"]["tokens"]["csrftoken"]

    yield session, csrf_token, url
    session.close()


@pytest.mark.abort_on_fail
def test_integrate_smtp(
    juju: jubilant.Juju,
    app: App,
    smtp_integrator: App,
):
    """Test that the SMTP relation can be established and charm goes active."""
    juju.integrate(f"{app.name}:smtp", f"{smtp_integrator.name}:smtp")
    juju.wait(
        lambda status: jubilant.all_active(status) and jubilant.all_agents_idle(status),
        successes=5,
        timeout=5 * 60,
    )


@pytest.mark.abort_on_fail
def test_send_email_via_api(
    juju: jubilant.Juju,
    app: App,
    namespace: str,
    authenticated_session: tuple,
    requests_timeout: int,
):
    """Send an email via the reset password action of the MediaWiki API and verify mailpit received it."""
    session, csrf_token, api_url = authenticated_session

    # Create a target user with an email address
    target_user = "TestEmailTarget"
    target_email = "target@example.com"

    # Create the recipient via maintenance script
    _exec_in_mediawiki(
        juju,
        app,
        (
            "php /var/www/html/w/maintenance/run.php createAndPromote "
            f"{target_user} testpass123 --email {target_email}"
        ),
    )

    resp = session.post(
        url=api_url,
        data={
            "action": "resetpassword",
            "email": target_email,
            "token": csrf_token,
            "format": "json",
        },
        timeout=requests_timeout,
    )
    assert resp.status_code == 200, f"resetpassword API returned {resp.status_code}: {resp.text}"
    result = resp.json()
    assert "error" not in result, f"resetpassword API error: {result}"
    assert result.get("resetpassword", {}).get("status") == "success"

    def _mailpit_has_message(_: jubilant.Status) -> bool:
        """Check if mailpit received the expected SMTP integration test message."""
        try:
            data = _query_mailpit(namespace, "/api/v1/messages")
            logger.info("Queried Mailpit messages: %s", data)
            for message in data.get("messages", []):
                assert message["From"]["Address"] == _SMTP_SENDER, (
                    f"Unexpected email sender: {message['From']['Address']}"
                )
                if (
                    "Account details on " in message.get("Subject", "")
                    and {"Address": target_email, "Name": target_user} in message["To"]
                ):
                    return True
            return False
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            return False

    juju.wait(_mailpit_has_message, delay=3, timeout=30, successes=1)


@pytest.mark.abort_on_fail
def test_smtp_relation_removal(
    juju: jubilant.Juju,
    app: App,
):
    """Test that removing the SMTP relation returns the charm to active."""
    juju.remove_relation(f"{app.name}:smtp", "smtp-integrator:smtp")
    juju.wait(
        lambda status: (
            jubilant.all_active(status)
            and jubilant.all_agents_idle(status)
            and "smtp" not in status.apps[app.name].relations
        ),
        successes=5,
        timeout=5 * 60,
    )

    # Verify SMTP config is removed
    output = _exec_in_mediawiki(
        juju,
        app,
        "cat /etc/mediawiki/LateSettings.php",
    )
    assert "$wgSMTP" not in output, "$wgSMTP should be removed after relation removal"
