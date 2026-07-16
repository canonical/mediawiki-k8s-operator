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
import requests
import urllib3
import yaml

from .types_ import App
from .utils import kubectl, req_okay


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
def local_settings() -> str:
    """The base local settings."""
    path = Path(__file__).parent / "test_data" / "LocalSettings.php"
    ls_contents = path.read_text()

    return ls_contents


@pytest.fixture(scope="module")
def ingress_address(traefik_lb_ip: str) -> str:
    """The address to use for accessing the application, based on the traefik load balancer IP."""
    return f"https://{traefik_lb_ip}"


@pytest.fixture(scope="module")
def app_config(local_settings, ingress_address) -> Generator[dict[str, Any], None, None]:
    """The base configuration to deploy with for the mediawiki application."""
    yield {
        "local-settings": local_settings,
        "url-origin": ingress_address,
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


@pytest.fixture(scope="session", autouse=True)
def disable_ssl_verification() -> Generator[None, None, None]:
    """Globally disable SSL certificate verification for all requests in integration tests.

    Self-signed certificates (SSC) are used throughout the test suite, so verification
    would always fail. Warnings are also suppressed to keep test output clean.
    """
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    original_request = requests.Session.request

    def _request_no_verify(self, *args, **kwargs):
        kwargs["verify"] = False
        return original_request(self, *args, **kwargs)

    requests.Session.request = _request_no_verify  # type: ignore[method-assign]
    yield
    requests.Session.request = original_request  # type: ignore[method-assign]


@pytest.fixture(scope="module", name="ssc")
def ssc_fixture(juju: jubilant.Juju, pytestconfig: pytest.Config) -> Generator[App, None, None]:
    """Deploy self-signed certificates charm and return its app information."""
    use_existing = pytestconfig.getoption("--use-existing", default=False)
    if use_existing:
        yield App(name="self-signed-certificates")
        return

    juju.deploy("self-signed-certificates", channel="1/stable")

    yield App(name="self-signed-certificates")


@pytest.fixture(scope="module", name="db")
def db_fixture(
    juju: jubilant.Juju, pytestconfig: pytest.Config, ssc: App
) -> Generator[App, None, None]:
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

    juju.integrate("mysql-k8s:certificates", f"{ssc.name}:certificates")

    yield App(name="mysql-k8s")


@pytest.fixture(scope="module", name="traefik")
def traefik_fixture(
    juju: jubilant.Juju, pytestconfig: pytest.Config, ssc: App
) -> Generator[App, None, None]:
    """Deploy traefik-k8s and return its app information."""
    use_existing = pytestconfig.getoption("--use-existing", default=False)
    if use_existing:
        yield App(name="traefik-k8s")
        return

    juju.deploy(
        "traefik-k8s",
        channel="latest/candidate",
        base="ubuntu@20.04",
        revision=298,  # 295 often errors on pebble start hook
        trust=True,
    )

    juju.integrate("traefik-k8s:certificates", f"{ssc.name}:certificates")

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


@pytest.fixture(scope="module", name="early_app")
def early_app_fixture(
    juju: jubilant.Juju,
    db: App,
    traefik: App,
    redis: App,
    metadata: Dict[str, Any],
    pytestconfig: pytest.Config,
    charm: str,
    charm_resources: Dict[str, str],
) -> Generator[App, None, None]:
    """Early MediaWiki charm used for integration testing.
    Builds the charm and deploys it and the relations it depends on.

    Non-blocking dependencies only such that dependencies are deployed together.
    """
    app_name = metadata["name"]

    use_existing = pytestconfig.getoption("--use-existing", default=False)
    if use_existing:
        yield App(name=app_name)
        return

    num_units = pytestconfig.getoption("--num-units")
    juju.deploy(
        charm=charm,
        app=app_name,
        resources=charm_resources,
        num_units=num_units,
    )

    juju.wait(
        lambda status: (
            jubilant.all_blocked(status, app_name)
            and jubilant.all_active(
                status,
                db.name,
                traefik.name,
                redis.name,
            )
        ),
        timeout=20 * 60,
        error=jubilant.any_error,
    )

    juju.integrate(app_name, traefik.name)
    juju.integrate(app_name, db.name)
    juju.integrate(app_name, redis.name)
    juju.wait(
        jubilant.all_active,
        timeout=10 * 60,
        error=jubilant.any_error,
    )

    yield App(name=app_name)


@pytest.fixture(scope="module", name="app")
def app_fixture(
    juju: jubilant.Juju,
    early_app: App,
    app_config: Dict[str, Any],
    ingress_address: str,
    requests_timeout: int,
) -> Generator[App, None, None]:
    """MediaWiki charm used for integration testing."""
    juju.config(early_app.name, app_config)
    juju.wait(
        lambda status: jubilant.all_active(status) and req_okay(ingress_address, requests_timeout),
        timeout=5 * 60,
        error=jubilant.any_error,
    )

    yield early_app


@pytest.fixture(scope="module", name="admin_credentials")
def admin_credentials_fixture(juju: jubilant.Juju, app: App) -> tuple[str, str]:
    """Create an admin user and return its credentials once per module."""
    action = juju.run(
        f"{app.name}/leader",
        "create-and-promote",
        {
            "username": "admin",
            "bureaucrat": True,
            "sysop": True,
            "force": True,
            "generate-password": True,
        },
    )
    assert action.status == "completed"
    return action.results["username"], action.results["password"]


@pytest.fixture(scope="module", name="authenticated_session")
def authenticated_session_fixture(
    requests_timeout: int,
    ingress_address: str,
    admin_credentials: tuple[str, str],
) -> Generator[tuple[requests.Session, str, str], None, None]:
    """Return an authenticated MediaWiki session with a CSRF token.

    Yields (session, csrf_token, api_url). The session is closed after use.
    """
    username, password = admin_credentials
    url = f"{ingress_address}/w/api.php"
    session = requests.Session()

    req = session.get(
        url=url,
        params={"action": "query", "meta": "tokens", "type": "login", "format": "json"},
        timeout=requests_timeout,
    )
    login_token = req.json()["query"]["tokens"]["logintoken"]

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

    req = session.get(
        url=url,
        params={"action": "query", "meta": "tokens", "format": "json"},
        timeout=requests_timeout,
    )
    csrf_token = req.json()["query"]["tokens"]["csrftoken"]

    yield session, csrf_token, url
    session.close()


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
