#!/usr/bin/env python3

# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for S3."""

import json
import logging
import shlex
import subprocess  # nosec: B404
from pathlib import Path
from typing import Generator
from urllib.parse import urlparse

import jubilant
import pytest
import requests
from minio import Minio

from .types_ import App
from .utils import juju_exec, kubectl

logger = logging.getLogger(__name__)

_S3_BUCKET_NAME = "mediawiki"
_MINIO_ACCESS_KEY = "access"
_MINIO_SECRET_KEY = "secretsecret"  # nosec: B105
_TEST_DATA = Path(__file__).parent / "test_data" / "s3_proxy"
_NO_PROXY = "127.0.0.1,localhost,::1,10.151.0.0/16,10.152.0.0/16,10.156.0.0/16,.svc,.cluster.local"
_UPSTREAM_DOWNLOAD_SHA256 = "8cb299f91d39c4ed973c768550bf4a97cd5ad62cf25e3f086d2821c3ad59ae62"


def _kubectl(juju: jubilant.Juju, *args: str, manifest: dict | None = None) -> str:
    """Run a test-scoped Kubernetes operation with optional structured input."""
    assert juju.model is not None
    return subprocess.run(  # nosec: B603
        kubectl(juju.model, *args),
        input=json.dumps(manifest) if manifest is not None else None,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.fixture(scope="module")
def s3_proxy(juju: jubilant.Juju) -> Generator[str, None, None]:
    """Provide an HTTP and CONNECT proxy before deploying MediaWiki."""
    manifest = _TEST_DATA / "forward-proxy.yaml"
    try:
        _kubectl(juju, "apply", "-f", str(manifest))
        _kubectl(juju, "wait", "--for=condition=Ready", "pod/s3-proxy", "--timeout=180s")
        yield f"http://s3-proxy.{juju.model}.svc.cluster.local:3128"
    except subprocess.CalledProcessError as exc:
        logger.error("S3 proxy setup failed: %s", exc.stderr)
        for args in (
            ("describe", "pod/s3-proxy"),
            ("logs", "pod/s3-proxy", "--tail=100"),
            ("logs", "pod/s3-proxy", "--previous", "--tail=100"),
        ):
            try:
                logger.error("S3 proxy diagnostics (%s):\n%s", args, _kubectl(juju, *args))
            except subprocess.CalledProcessError as diagnostic_error:
                logger.error("Could not collect %s: %s", args, diagnostic_error.stderr)
        raise
    finally:
        _kubectl(juju, "delete", "-f", str(manifest), "--ignore-not-found")


@pytest.fixture(scope="module")
def model_config_override(s3_proxy: str) -> dict[str, str]:
    """Configure both model proxies before application deployment, as in SMTP tests."""
    return {
        "juju-http-proxy": s3_proxy,
        "juju-https-proxy": s3_proxy,
        "juju-no-proxy": _NO_PROXY,
    }


@pytest.fixture(scope="module")
def s3_endpoint(juju: jubilant.Juju, minio: App) -> Generator[str, None, None]:
    """Expose MinIO outside cluster DNS bypass rules using its real service selector."""
    juju.wait(lambda status: jubilant.all_active(status, minio.name))
    service = json.loads(_kubectl(juju, "get", "service", minio.name, "-o=json"))
    manifest = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "s3-proxy-target"},
        "spec": {
            "type": "LoadBalancer",
            "selector": service["spec"]["selector"],
            "ports": [{"port": 9000, "targetPort": 9000}],
        },
    }
    try:
        _kubectl(juju, "apply", "-f", "-", manifest=manifest)

        def address() -> str:
            """Read the allocated load-balancer address."""
            return _kubectl(
                juju,
                "get",
                "service/s3-proxy-target",
                "-o=jsonpath={.status.loadBalancer.ingress[0].ip}",
            ).strip()

        juju.wait(lambda _: bool(address()), delay=3, timeout=120, successes=1)
        endpoint = f"http://{address()}:9000"
        assert not requests.utils.should_bypass_proxies(endpoint, _NO_PROXY), (
            "The S3 proxy-test destination must be outside NO_PROXY networks"
        )
        yield endpoint
    finally:
        _kubectl(juju, "delete", "service/s3-proxy-target", "--ignore-not-found")


@pytest.fixture(scope="module")
def s3_proxy_policy(juju: jubilant.Juju, minio: App, s3_proxy: str) -> Generator[None, None, None]:
    """After bucket setup, allow S3 ingress only from the proxy pod."""
    del s3_proxy
    service = json.loads(_kubectl(juju, "get", "service", minio.name, "-o=json"))
    manifest = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "s3-through-proxy"},
        "spec": {
            "podSelector": {"matchLabels": service["spec"]["selector"]},
            "policyTypes": ["Ingress"],
            "ingress": [
                {
                    "from": [{"podSelector": {"matchLabels": {"app": "s3-proxy"}}}],
                    "ports": [{"protocol": "TCP", "port": 9000}],
                }
            ],
        },
    }
    try:
        _kubectl(juju, "apply", "-f", "-", manifest=manifest)
        yield
    finally:
        _kubectl(juju, "delete", "networkpolicy/s3-through-proxy", "--ignore-not-found")


@pytest.fixture(scope="module", name="minio")
def minio_fixture(juju: jubilant.Juju, pytestconfig: pytest.Config) -> Generator[App, None, None]:
    """Deploy minio and return its app information."""
    use_existing = pytestconfig.getoption("--use-existing", default=False)
    if use_existing:
        yield App(name="minio")
        return

    juju.deploy(
        "minio",
        channel="ckf-1.10/stable",
        config={"access-key": _MINIO_ACCESS_KEY, "secret-key": _MINIO_SECRET_KEY},
    )

    yield App(name="minio")


@pytest.fixture(scope="module", name="s3_integrator")
def s3_integrator_fixture(
    juju: jubilant.Juju, pytestconfig: pytest.Config, s3_endpoint: str
) -> Generator[App, None, None]:
    """Deploy s3 integrator and return its app information."""
    use_existing = pytestconfig.getoption("--use-existing", default=False)
    if use_existing:
        yield App(name="s3-integrator")
        return

    secret_uri = juju.add_secret(
        "s3-credentials",
        {
            "access-key": _MINIO_ACCESS_KEY,
            "secret-key": _MINIO_SECRET_KEY,
        },
    )

    juju.deploy(
        "s3-integrator",
        channel="2/edge",
        config={
            "bucket": _S3_BUCKET_NAME,
            "endpoint": s3_endpoint,
            "credentials": secret_uri,
            "s3-uri-style": "path",
        },
    )

    juju.grant_secret(secret_uri, "s3-integrator")

    yield App(name="s3-integrator")


@pytest.mark.abort_on_fail
def test_integrate_s3_integrator_with_mediawiki(
    juju: jubilant.Juju,
    app: App,
    local_settings: str,
    s3_integrator: App,
    minio: App,
):
    """Prepare the S3 bucket and integrate the S3 integrator with MediaWiki."""
    juju.wait(lambda status: jubilant.all_active(status, minio.name))

    status = juju.status()
    minio_address = status.apps["minio"].units["minio/0"].address
    mc_client = Minio(
        f"{minio_address}:9000",
        access_key=_MINIO_ACCESS_KEY,
        secret_key=_MINIO_SECRET_KEY,
        secure=False,
    )
    found = mc_client.bucket_exists(_S3_BUCKET_NAME)
    if not found:
        mc_client.make_bucket(_S3_BUCKET_NAME)
        # Allow anonymous read access to the bucket
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": "*"},
                    "Action": ["s3:GetBucketLocation", "s3:GetObject"],
                    "Resource": [
                        f"arn:aws:s3:::{_S3_BUCKET_NAME}",
                        f"arn:aws:s3:::{_S3_BUCKET_NAME}/*",
                    ],
                }
            ],
        }
        mc_client.set_bucket_policy(_S3_BUCKET_NAME, json.dumps(policy))

    local_settings += f"$wgAWSBucketDomain = '{minio_address}:9000/$1';\n"
    juju.config(app.name, {"local-settings": local_settings})

    juju.integrate(app.name, s3_integrator.name)

    juju.wait(
        jubilant.all_active,
    )


@pytest.mark.abort_on_fail
def test_s3_backend_compatibility(juju: jubilant.Juju, app: App):
    """Require review if the installed upstream download method changes."""
    probe = _TEST_DATA / "backend-check.php"
    remote = juju_exec(juju, app, "mktemp --suffix=.php").strip()
    juju.scp(str(probe), f"{app.name}/leader:{remote}", container="mediawiki")
    try:
        result = json.loads(
            juju_exec(juju, app, f"php /var/www/html/w/maintenance/run.php {shlex.quote(remote)}")
        )
    finally:
        juju_exec(juju, app, f"rm -f {shlex.quote(remote)}")
    assert result["backend"] == "CharmS3FileBackend"
    assert result["protected"] and result["parameters"] == 1
    assert result["curl_version"] >= 0x075600
    assert result["upstream_sha256"] == _UPSTREAM_DOWNLOAD_SHA256, (
        "Review upstream getLocalCopyCached and the charm adapter before updating this fingerprint"
    )


@pytest.mark.abort_on_fail
@pytest.mark.usefixtures("s3_proxy_policy")
def test_upload(
    juju: jubilant.Juju,
    app: App,
    s3_endpoint: str,
    requests_timeout: int,
    authenticated_session: tuple[requests.Session, str, str],
):
    """Check uploading a file to MediaWiki via the API."""
    juju.wait(jubilant.all_active)

    probe = (
        "$handle = curl_init(" + json.dumps(s3_endpoint + "/minio/health/live") + ");"
        "curl_setopt_array($handle, [CURLOPT_PROXY => '', CURLOPT_CONNECTTIMEOUT => 2, "
        "CURLOPT_TIMEOUT => 3, CURLOPT_RETURNTRANSFER => true]);"
        "echo curl_exec($handle) === false ? 'blocked' : 'direct';"
    )
    juju.wait(
        lambda _: juju_exec(juju, app, "php -r " + shlex.quote(probe)).strip() == "blocked",
        delay=1,
        timeout=30,
        successes=1,
    )

    session, csrf_token, url = authenticated_session

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

    assert req.status_code == 200, f"Expected status code 200, got {req.status_code}"
    response = req.json()
    assert response.get("upload", {}).get("result") == "Success", f"S3 upload failed: {response}"
    proxy_log = _kubectl(juju, "logs", "pod/s3-proxy")
    hostname = urlparse(s3_endpoint).hostname
    assert hostname is not None and hostname in proxy_log


@pytest.mark.abort_on_fail
def test_clamav(
    juju: jubilant.Juju,
    requests_timeout: int,
    authenticated_session: tuple[requests.Session, str, str],
):
    """Check that ClamAV is working by uploading a test file containing the EICAR test signature."""
    juju.wait(jubilant.all_active)

    session, csrf_token, url = authenticated_session

    eicar_test_string = "X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
    req = session.post(
        url=url,
        data={
            "action": "upload",
            "filename": "EICAR-Test-File.png",
            "token": csrf_token,
            "format": "json",
            "ignorewarnings": 1,
        },
        files={
            "file": (
                "EICAR-Test-File.png",
                bytes(eicar_test_string, "utf-8"),
                "multipart/form-data",
            )
        },
        timeout=requests_timeout,
    )

    logger.info("ClamAV upload response: %s", req.text)
    assert req.status_code == 200, f"Expected status code 200, got {req.status_code}"
    assert req.json()["error"]["code"] == "verification-error", (
        f"Expected error code to be 'verification-error', got {req.json()['error']['code']}"
    )
    assert " Eicar-Test-Signature FOUND" in req.json()["error"]["details"], (
        f"Expected error details to contain ' Eicar-Test-Signature FOUND', got {req.json()['error']['details']}"
    )
