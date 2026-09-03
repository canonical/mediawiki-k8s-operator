# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for SMTP through an HTTP CONNECT proxy."""

import json
import logging
import subprocess  # nosec: B404
import textwrap
from collections.abc import Generator
from pathlib import Path
from typing import Any

import jubilant
import pytest

from .types_ import App
from .utils import juju_exec, kubectl

logger = logging.getLogger(__name__)

_MAILPIT_SMTP_PORT = 1025
_MAILPIT_HTTP_PORT = 8025
_PROXY_PORT = 3128
_SMTP_USER = "mediawiki"
_SMTP_PASSWORD = "smtp-test-password"  # nosec: B105
_SMTP_SENDER = "mediawiki@example.com"
_NO_PROXY = (
    "127.0.0.1,localhost,::1,10.151.0.0/16,10.152.0.0/16,10.156.0.0/16,"
    "10.158.0.0/16,.svc,.cluster.local,10.85.0.0/16,10.86.0.0/16"
)
_TEST_DATA = Path(__file__).parent / "test_data" / "smtp_proxy"


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


def _apply_manifest(namespace: str, manifest: str) -> None:
    """Apply a Kubernetes manifest to the Juju model namespace."""
    subprocess.run(  # nosec: B603
        kubectl(namespace, "apply", "-f", "-"),
        input=manifest,
        capture_output=True,
        text=True,
        check=True,
    )


def _query_mailpit(namespace: str) -> dict[str, Any]:
    """Query the Mailpit HTTP API via kubectl exec."""
    result = _kubectl_run(
        namespace,
        "exec",
        "pod/mailpit-proxy-test",
        "--",
        "wget",
        "--header=Accept: application/json",
        "-q",
        "-O-",
        f"http://localhost:{_MAILPIT_HTTP_PORT}/api/v1/messages",
    )
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def mailpit_load_balancer(juju: jubilant.Juju) -> Generator[str, None, None]:
    """Deploy Mailpit with its SMTP port exposed through a LoadBalancer service."""
    assert juju.model is not None, "Juju model must be set"
    namespace = juju.model
    _apply_manifest(namespace, (_TEST_DATA / "mailpit-load-balancer.yaml").read_text())
    _kubectl_run(
        namespace,
        "wait",
        "--for=condition=Ready",
        "pod/mailpit-proxy-test",
        "--timeout=120s",
    )

    def _load_balancer_ready(_: jubilant.Status) -> bool:
        result = _kubectl_run(
            namespace,
            "get",
            "service/mailpit-smtp-proxy-test",
            "-o=jsonpath={.status.loadBalancer.ingress[0].ip}",
        )
        return bool(result.stdout.strip())

    juju.wait(_load_balancer_ready, delay=3, timeout=120, successes=1)
    address = _kubectl_run(
        namespace,
        "get",
        "service/mailpit-smtp-proxy-test",
        "-o=jsonpath={.status.loadBalancer.ingress[0].ip}",
    ).stdout.strip()
    yield address
    _kubectl_run(namespace, "delete", "service/mailpit-smtp-proxy-test", "--ignore-not-found")
    _kubectl_run(namespace, "delete", "pod/mailpit-proxy-test", "--ignore-not-found")


@pytest.fixture(scope="module")
def connect_proxy(juju: jubilant.Juju) -> Generator[str, None, None]:
    """Deploy a minimal HTTP CONNECT proxy and yield its in-cluster URL."""
    assert juju.model is not None, "Juju model must be set"
    namespace = juju.model
    _apply_manifest(namespace, (_TEST_DATA / "smtp-connect-proxy.yaml").read_text())
    _kubectl_run(
        namespace,
        "wait",
        "--for=condition=Ready",
        "pod/smtp-connect-proxy",
        "--timeout=120s",
    )
    yield f"http://smtp-connect-proxy.{namespace}.svc.cluster.local:{_PROXY_PORT}"
    _kubectl_run(namespace, "delete", "service/smtp-connect-proxy", "--ignore-not-found")
    _kubectl_run(namespace, "delete", "pod/smtp-connect-proxy", "--ignore-not-found")
    _kubectl_run(namespace, "delete", "configmap/smtp-connect-proxy-script", "--ignore-not-found")


@pytest.fixture(scope="module")
def mailpit_smtp_policy(
    juju: jubilant.Juju,
    connect_proxy: str,
    mailpit_load_balancer: str,
) -> Generator[None, None, None]:
    """Allow Mailpit SMTP connections only from the HTTP CONNECT proxy pod."""
    del connect_proxy, mailpit_load_balancer
    assert juju.model is not None, "Juju model must be set"
    namespace = juju.model
    manifest = textwrap.dedent(f"""\
        apiVersion: networking.k8s.io/v1
        kind: NetworkPolicy
        metadata:
          name: mailpit-smtp-through-proxy
        spec:
          podSelector:
            matchLabels:
              app: mailpit-proxy-test
          policyTypes:
          - Ingress
          ingress:
          - from:
            - podSelector:
                matchLabels:
                  app: smtp-connect-proxy
            ports:
            - protocol: TCP
              port: {_MAILPIT_SMTP_PORT}
    """)
    _apply_manifest(namespace, manifest)
    yield
    _kubectl_run(
        namespace, "delete", "networkpolicy/mailpit-smtp-through-proxy", "--ignore-not-found"
    )


@pytest.fixture(scope="module")
def model_config_override(connect_proxy: str) -> Generator[dict[str, str], None, None]:
    """Configure the model proxy before the MediaWiki application is deployed."""
    yield {"juju-http-proxy": connect_proxy, "juju-no-proxy": _NO_PROXY}


@pytest.fixture(scope="module")
def smtp_integrator(
    juju: jubilant.Juju,
    app: App,
    mailpit_load_balancer: str,
    mailpit_smtp_policy: None,
    pytestconfig: pytest.Config,
) -> Generator[App, None, None]:
    """Deploy smtp-integrator configured to reach Mailpit's external address."""
    del mailpit_smtp_policy
    if pytestconfig.getoption("--use-existing", default=False):
        yield App(name="smtp-integrator")
        return

    secret_uri = juju.add_secret("smtp-proxy-test-credentials", {"password": _SMTP_PASSWORD})
    juju.deploy(
        "smtp-integrator",
        channel="latest/stable",
        config={
            "host": mailpit_load_balancer,
            "port": str(_MAILPIT_SMTP_PORT),
            "transport_security": "tls",
            "auth_type": "plain",
            "user": _SMTP_USER,
            "password_secret": secret_uri,
            "skip_ssl_verify": "true",
            "smtp_sender": _SMTP_SENDER,
        },
    )
    juju.grant_secret(secret_uri, ["smtp-integrator", app.name])
    juju.wait(lambda status: jubilant.all_active(status, "smtp-integrator"))
    yield App(name="smtp-integrator")


@pytest.mark.abort_on_fail
def test_integrate_smtp_through_proxy(
    juju: jubilant.Juju,
    app: App,
    smtp_integrator: App,
):
    """Integrate SMTP after MediaWiki has been deployed with an HTTP proxy."""
    juju.integrate(f"{app.name}:smtp", f"{smtp_integrator.name}:smtp")

    def _smtp_settings_use_tunnel(_: jubilant.Status) -> bool:
        try:
            settings = juju_exec(juju, app, "cat /etc/mediawiki/LateSettings.php")
        except subprocess.CalledProcessError:
            return False
        return "'host' => 'ssl://127.0.0.1'" in settings and "'port' => 8125" in settings

    juju.wait(
        _smtp_settings_use_tunnel,
        error=jubilant.any_error,
        delay=3,
        timeout=5 * 60,
    )


@pytest.mark.abort_on_fail
def test_smtp_uses_http_connect_proxy(
    juju: jubilant.Juju,
    app: App,
    authenticated_session: tuple,
    connect_proxy: str,
    mailpit_load_balancer: str,
    requests_timeout: int,
):
    """Send a reset email through the SMTP HTTP CONNECT proxy."""
    # The tunneled host/port are already verified deterministically by
    # test_integrate_smtp_through_proxy; here we only check what that test doesn't cover.
    settings = juju_exec(juju, app, "cat /etc/mediawiki/LateSettings.php")
    # Guards the juju-http-proxy -> JUJU_CHARM_HTTP_PROXY -> $wgHttpProxy contract, so that
    # a proxy the charm never received is reported as such instead of as a wrong SMTP host.
    assert f"$wgHttpProxy = '{connect_proxy}';" in settings
    assert f"'peer_name' => '{mailpit_load_balancer}'" in settings

    smtp_proxy_status = juju_exec(juju, app, "pebble services smtp-proxy")
    assert "smtp-proxy" in smtp_proxy_status
    assert "active" in smtp_proxy_status

    session, csrf_token, api_url = authenticated_session
    target_user = "ProxyEmailTarget"
    target_email = "proxy-target@example.com"
    juju_exec(
        juju,
        app,
        (
            "php /var/www/html/w/maintenance/run.php createAndPromote "
            f"{target_user} testpass123 --email {target_email}"
        ),
    )
    response = session.post(
        url=api_url,
        data={
            "action": "resetpassword",
            "email": target_email,
            "token": csrf_token,
            "format": "json",
        },
        timeout=requests_timeout,
    )
    assert response.status_code == 200, response.text
    assert response.json().get("resetpassword", {}).get("status") == "success"

    assert juju.model is not None, "Juju model must be set"
    namespace = juju.model

    def _message_received(_: jubilant.Status) -> bool:
        try:
            for message in _query_mailpit(namespace).get("messages", []):
                if (
                    message["From"]["Address"] == _SMTP_SENDER
                    and "Account details on " in message.get("Subject", "")
                    and {"Address": target_email, "Name": target_user} in message["To"]
                ):
                    return True
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            return False
        return False

    juju.wait(_message_received, delay=3, timeout=30, successes=1)
    proxy_logs = _kubectl_run(namespace, "logs", "pod/smtp-connect-proxy").stdout
    assert f"CONNECT {mailpit_load_balancer}:{_MAILPIT_SMTP_PORT}" in proxy_logs


@pytest.mark.abort_on_fail
def test_smtp_proxy_relation_removal(juju: jubilant.Juju, app: App):
    """Remove the SMTP relation after exercising the proxy route."""
    juju.remove_relation(f"{app.name}:smtp", "smtp-integrator:smtp")

    def _smtp_proxy_torn_down(_: jubilant.Status) -> bool:
        try:
            settings = juju_exec(juju, app, "cat /etc/mediawiki/LateSettings.php")
            smtp_proxy_status = juju_exec(juju, app, "pebble services smtp-proxy")
        except subprocess.CalledProcessError:
            return False
        return "$wgSMTP" not in settings and "inactive" in smtp_proxy_status

    juju.wait(
        _smtp_proxy_torn_down,
        error=jubilant.any_error,
        delay=3,
        timeout=5 * 60,
    )
