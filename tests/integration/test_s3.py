#!/usr/bin/env python3

# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for S3."""

import json
import logging
from pathlib import Path
from typing import Generator

import jubilant
import pytest
import requests
from minio import Minio

from .types_ import App

logger = logging.getLogger(__name__)

_S3_BUCKET_NAME = "mediawiki"
_MINIO_ACCESS_KEY = "access"
_MINIO_SECRET_KEY = "secretsecret"  # nosec: B105


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
    juju: jubilant.Juju, pytestconfig: pytest.Config
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
            "endpoint": f"http://minio.{juju.model}.svc.cluster.local:9000",
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

    juju.wait(
        jubilant.all_active,
    )

    juju.integrate(app.name, s3_integrator.name)


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
