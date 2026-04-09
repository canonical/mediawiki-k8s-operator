# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Fixtures for charm integration tests."""

import json
import os
import subprocess  # nosec: B404 # We control inputs in integration tests
import typing
from pathlib import Path
from typing import Any, Dict, Generator

import jubilant
import pytest
import yaml
from minio import Minio

from .types_ import App
from .utils import kubectl

_S3_BUCKET_NAME = "mediawiki"
_MINIO_ACCESS_KEY = "access"
_MINIO_SECRET_KEY = "secretsecret"  # nosec: B105


@pytest.fixture(scope="module", name="charm")
def charm_fixture(pytestconfig: pytest.Config, metadata: Dict[str, Any]) -> str:
    """Get value from parameter charm-file, otherwise packing the charm and returning the filename."""
    charm = pytestconfig.getoption("--charm-file")
    use_existing = pytestconfig.getoption("--use-existing", default=False)

    if charm or use_existing:
        return charm

    try:
        subprocess.run(["charmcraft", "pack"], check=True, capture_output=True, text=True)  # nosec B603, B607
    except subprocess.CalledProcessError as exc:
        raise OSError(f"Error packing charm: {exc}; Stderr:\n{exc.stderr}") from None

    app_name = metadata["name"]
    charm_path = Path(__file__).parent.parent.parent
    charms = [p.absolute() for p in charm_path.glob(f"{app_name}_*.charm")]
    assert charms, f"{app_name} .charm file not found"
    assert len(charms) == 1, f"{app_name} has more than one .charm file, unsure which to use"
    return str(charms[0])


@pytest.fixture(scope="module")
def charm_resources(pytestconfig: pytest.Config, metadata: Dict[str, Any]) -> dict[str, str]:
    """The OCI resources for the charm, read from option or env vars."""
    resources = {"git-sync-image": metadata["resources"]["git-sync-image"]["upstream-source"]}

    mediawiki_image = pytestconfig.getoption("--mediawiki-image")
    if mediawiki_image:
        resources["mediawiki-image"] = mediawiki_image
        return resources

    resource_name = os.environ.get("OCI_RESOURCE_NAME")
    rock_image_uri = os.environ.get("ROCK_IMAGE")

    if not resource_name or not rock_image_uri:
        pytest.fail(
            "Environment variables OCI_RESOURCE_NAME and/or ROCK_IMAGE are not set. "
            "Please set '--mediawiki-image' or run tests via 'make integration'."
        )

    resources[resource_name] = rock_image_uri
    return resources


@pytest.fixture(scope="session")
def metadata():
    """Pytest fixture to load charm metadata."""
    return yaml.safe_load(Path("./charmcraft.yaml").read_text())


@pytest.fixture(scope="session")
def requests_timeout():
    """Provides a global default timeout for HTTP requests"""
    return 15


@pytest.fixture(scope="module")
def local_settings(juju: jubilant.Juju, minio: App) -> Generator[str, None, None]:
    """The base local settings."""
    path = Path(__file__).parent / "test_data" / "LocalSettings.php"
    ls_contents = path.read_text()

    status = juju.status()
    minio_address = status.apps[minio.name].units[f"{minio.name}/0"].address
    ls_contents += f"$wgAWSBucketDomain = '{minio_address}:9000/$1';\n"

    yield ls_contents


@pytest.fixture(scope="module")
def ingress_address(traefik_lb_ip: str) -> str:
    """The address to use for accessing the application, based on the traefik load balancer IP."""
    return f"http://{traefik_lb_ip}"


@pytest.fixture(scope="module")
def app_config(local_settings, traefik_lb_ip) -> Generator[dict[str, Any], None, None]:
    """The base configuration to deploy with for the mediawiki application."""
    yield {
        "local-settings": local_settings,
        "url-origin": f"//{traefik_lb_ip}",
    }


@pytest.fixture(scope="session", name="juju")
def juju_fixture(request: pytest.FixtureRequest) -> Generator[jubilant.Juju, None, None]:
    """Pytest fixture that wraps :meth:`jubilant.with_model`."""

    def show_debug_log(juju: jubilant.Juju):
        """Show debug log.

        Args:
            juju: the Juju object.
        """
        if request.session.testsfailed:
            log = juju.debug_log(limit=1000)
            print(log, end="")

    use_existing = request.config.getoption("--use-existing", default=False)
    if use_existing:
        juju = jubilant.Juju()
        yield juju
        show_debug_log(juju)
        return

    model = request.config.getoption("--model")
    if model:
        juju = jubilant.Juju(model=model)
        yield juju
        show_debug_log(juju)
        return

    keep_models = typing.cast(bool, request.config.getoption("--keep-models"))
    with jubilant.temp_model(keep=keep_models) as juju:
        juju.wait_timeout = 10 * 60
        yield juju
        show_debug_log(juju)
        return


@pytest.fixture(scope="module", name="db")
def db_fixture(juju: jubilant.Juju, pytestconfig: pytest.Config) -> Generator[App, None, None]:
    """Deploy the database charm and return its app information."""
    use_existing = pytestconfig.getoption("--use-existing", default=False)
    if use_existing:
        yield App(name="mysql-k8s")
        return

    juju.deploy(
        "mysql-k8s",
        channel="8.0/stable",
        base="ubuntu@22.04",
        trust=True,
        config={"profile": "testing"},
    )
    yield App(name="mysql-k8s")


@pytest.fixture(scope="module", name="traefik")
def traefik_fixture(
    juju: jubilant.Juju, pytestconfig: pytest.Config
) -> Generator[App, None, None]:
    """Deploy traefik-k8s and return its app information."""
    use_existing = pytestconfig.getoption("--use-existing", default=False)
    if use_existing:
        yield App(name="traefik-k8s")
        return

    juju.deploy(
        "traefik-k8s",
        channel="latest/stable",
        base="ubuntu@20.04",
        trust=True,
    )
    yield App(name="traefik-k8s")


@pytest.fixture(scope="module")
def traefik_lb_ip(juju: jubilant.Juju, traefik: App) -> Generator[str, None, None]:
    """Get the LoadBalancer external IP for the traefik-k8s-lb service."""
    juju.wait(lambda status: jubilant.all_active(status, traefik.name), timeout=5 * 60)

    result = subprocess.run(  # nosec: B603 # We control inputs in integration tests
        kubectl(juju.model, "get", f"service/{traefik.name}-lb", "-o=jsonpath='{}'"),
        capture_output=True,
        text=True,
        check=True,
    )

    if result.returncode != 0:
        raise RuntimeError(f"Failed to get LoadBalancer info: {result.stderr}")
    result = json.loads(result.stdout.strip("'"))

    yield result["status"]["loadBalancer"]["ingress"][0]["ip"]


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

    juju.wait(lambda status: jubilant.all_active(status, "minio"))

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

    yield App(name="minio")


@pytest.fixture(scope="module", name="redis")
def redis_fixture(juju: jubilant.Juju, pytestconfig: pytest.Config) -> Generator[App, None, None]:
    """Deploy redis and return its app information."""
    use_existing = pytestconfig.getoption("--use-existing", default=False)
    if use_existing:
        yield App(name="redis-k8s")
        return

    juju.deploy(
        "redis-k8s",
        channel="latest/edge",
    )
    yield App(name="redis-k8s")


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


@pytest.fixture(scope="module", name="app")
def app_fixture(
    juju: jubilant.Juju,
    db: App,
    traefik: App,
    minio: App,
    redis: App,
    s3_integrator: App,
    metadata: Dict[str, Any],
    app_config: Dict[str, Any],
    pytestconfig: pytest.Config,
    charm: str,
    charm_resources: Dict[str, str],
) -> Generator[App, None, None]:
    """MediaWiki charm used for integration testing.
    Builds the charm and deploys it and the relations it depends on.
    """
    app_name = metadata["name"]

    use_existing = pytestconfig.getoption("--use-existing", default=False)
    if use_existing:
        yield App(name=app_name)
        return

    juju.deploy(
        charm=charm,
        app=app_name,
        config=app_config,
        resources=charm_resources,
        num_units=3,
        base="ubuntu@24.04",
    )

    juju.wait(
        lambda status: (
            jubilant.all_blocked(status, app_name)
            and jubilant.all_active(
                status,
                db.name,
                traefik.name,
                minio.name,
                redis.name,
                s3_integrator.name,
            )
        ),
        timeout=20 * 60,
    )

    juju.integrate(app_name, traefik.name)
    juju.integrate(app_name, db.name)
    juju.integrate(app_name, redis.name)
    juju.integrate(app_name, s3_integrator.name)
    juju.wait(jubilant.all_active)

    yield App(name=app_name)


@pytest.fixture(scope="module", name="ssh_key_secret")
def ssh_key_secret_fixture(juju: jubilant.Juju, app: App) -> Generator[str, None, None]:
    """Fixture to provide the SSH key secret for the MediaWiki application.

    The private keys are fake and are not authorized for use anywhere.
    """
    secret_content = {"mediawiki": "fake-private-key-for-mediawiki"}
    secret_uri = juju.add_secret("ssh-key-secret", secret_content)
    juju.grant_secret(secret_uri, app.name)
    yield secret_uri


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Pytest hook wrapper to set the test's rep_* attribute for abort_on_fail."""
    _ = call  # unused argument
    outcome = yield
    rep = outcome.get_result()
    setattr(item, "rep_" + rep.when, rep)


@pytest.fixture(autouse=True)
def abort_on_fail(request: pytest.FixtureRequest):
    """Fixture which aborts other tests in module after first fails."""
    abort_on_fail = request.node.get_closest_marker("abort_on_fail")
    if abort_on_fail and getattr(request.module, "__aborted__", False):
        pytest.xfail("abort_on_fail")

    _ = yield

    if abort_on_fail and request.node.rep_call.failed:
        request.module.__aborted__ = True
