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

from .types_ import App


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
def charm_resources(pytestconfig: pytest.Config) -> dict[str, str]:
    """The OCI resources for the charm, read from option or env vars."""
    mediawiki_image = pytestconfig.getoption("--mediawiki-image")
    if mediawiki_image:
        return {
            "mediawiki-image": mediawiki_image,
        }

    resource_name = os.environ.get("OCI_RESOURCE_NAME")
    rock_image_uri = os.environ.get("ROCK_IMAGE")

    if not resource_name or not rock_image_uri:
        pytest.fail(
            "Environment variables OCI_RESOURCE_NAME and/or ROCK_IMAGE are not set. "
            "Please set '--mediawiki-image' or run tests via 'make integration'."
        )

    return {resource_name: rock_image_uri}


@pytest.fixture(scope="session")
def metadata():
    """Pytest fixture to load charm metadata."""
    return yaml.safe_load(Path("./charmcraft.yaml").read_text())


@pytest.fixture(scope="session")
def requests_timeout():
    """Provides a global default timeout for HTTP requests"""
    return 15


@pytest.fixture(scope="session")
def local_settings() -> str:
    """The base local settings."""
    path = Path(__file__).parent / "test_data" / "LocalSettings.php"
    return path.read_text()


@pytest.fixture(scope="module")
def ingress_address(traefik_lb_ip: str) -> str:
    """The address to use for accessing the application, based on the traefik load balancer IP."""
    return f"http://{traefik_lb_ip}"


@pytest.fixture(scope="module")
def app_config(local_settings, traefik_lb_ip) -> dict[str, Any]:
    """The base configuration to deploy with for the mediawiki application."""
    return {
        "local-settings": local_settings,
        "hostname": traefik_lb_ip,
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

    command = ["kubectl"]
    if juju.model:
        command.extend(["-n", juju.model])
    command.extend(
        [
            "get",
            f"service/{traefik.name}-lb",
            "-o=jsonpath='{}'",
        ]
    )

    result = subprocess.run(  # nosec: B603 # We control inputs in integration tests
        command,
        capture_output=True,
        text=True,
        check=True,
    )

    if result.returncode != 0:
        raise RuntimeError(f"Failed to get LoadBalancer info: {result.stderr}")
    result = json.loads(result.stdout.strip("'"))

    yield result["status"]["loadBalancer"]["ingress"][0]["ip"]


@pytest.fixture(scope="module", name="app")
def app_fixture(
    juju: jubilant.Juju,
    db: App,
    traefik: App,
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
        base="ubuntu@24.04",
    )

    juju.wait(
        lambda status: (
            jubilant.all_blocked(status, app_name)
            and jubilant.all_active(status, db.name, traefik.name)
        ),
        timeout=20 * 60,
    )

    juju.integrate(app_name, traefik.name)
    juju.integrate(app_name, db.name)
    juju.wait(jubilant.all_active)

    yield App(name=app_name)
