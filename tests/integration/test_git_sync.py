#!/usr/bin/env python3

# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for the git-sync sidecar."""

import logging
import subprocess  # nosec: B404 # We control inputs in integration tests
import tempfile
import time
from pathlib import Path
from typing import Generator

import jubilant
import pytest
import requests

from .types_ import App
from .utils import kubectl, req_okay

logger = logging.getLogger(__name__)

_TEST_DATA = Path(__file__).parent / "test_data"
_GIT_SSH_SERVER_POD = "git-ssh-server"
_REPO_PATH = "/srv/repo.git"
_STATIC_ASSETS_PATH = "/var/www/html/static"
_STATIC_ASSETS_URL_PATH = "/static"


def _path_exists(
    juju: jubilant.Juju, app: App, path: str, container: str = "mediawiki"
) -> bool | None:
    """Return whether *path* exists in the mediawiki container.

    Args:
        juju: The Juju controller to use for SSH.
        app: The application whose container to check.
        path: The path to check for existence.
        container: The name of the container to check in. Defaults to "mediawiki".

    Returns:
        True if the path exists, False if it does not, or None if the container could not be reached.
    """
    try:
        result = juju.ssh(
            f"{app.name}/0",
            f"sh -c 'test -e {path} && echo exists || echo absent'",
            container=container,
        )
    except Exception:
        return None
    return "exists" in result


def _wait_for_file_contents(juju: jubilant.Juju, app: App, path: str, expected: str) -> None:
    """Wait until the file at *path* in the mediawiki container contains *expected*."""

    def check(_) -> bool:
        try:
            result = juju.ssh(f"{app.name}/0", f"cat {path}", container="mediawiki")
        except Exception:
            return False
        return expected in result

    juju.wait(check, delay=5, timeout=60 * 5, successes=2)


def _wait_for_path_exists(
    juju: jubilant.Juju, app: App, path: str, *, exists: bool = True
) -> None:
    """Wait until *path* in the mediawiki container exists (or doesn't, if *exists* is False).

    Follows symlinks: a broken symlink counts as not existing.
    SSH connectivity errors are treated as not yet satisfied.
    """
    juju.wait(
        lambda _: _path_exists(juju, app, path) == exists,
        delay=5,
        timeout=60 * 5,
        successes=2,
    )


@pytest.fixture(scope="module")
def git_ssh_server(juju: jubilant.Juju) -> Generator[dict[str, str], None, None]:
    """Deploy an ephemeral SSH git server pod and yield connection details.

    Yields a dict with keys: ip, private_key, host_keys, repo_url.
    """
    namespace = juju.model

    # Generate an ephemeral Ed25519 key pair
    with tempfile.TemporaryDirectory() as tmpdir:
        key_path = Path(tmpdir) / "id_ed25519"
        subprocess.run(  # nosec: B603
            ["/usr/bin/ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-q"],
            check=True,
        )
        private_key = key_path.read_text()
        public_key = key_path.with_suffix(".pub").read_text().strip()

    # Load the manifest template and substitute placeholders
    manifest_template = (_TEST_DATA / "git-ssh-server-pod.yaml").read_text()
    manifest = manifest_template.replace("__GIT_SSH_PUBLIC_KEY__", public_key)

    # Apply the pod manifest
    subprocess.run(  # nosec: B603
        kubectl(namespace, "apply", "-f", "-"),
        input=manifest,
        text=True,
        check=True,
        capture_output=True,
    )

    # Wait for the pod to be ready
    try:
        subprocess.run(  # nosec: B603
            kubectl(
                namespace,
                "wait",
                "--for=condition=Ready",
                f"pod/{_GIT_SSH_SERVER_POD}",
                "--timeout=300s",
            ),
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError:
        describe = subprocess.run(  # nosec: B603
            kubectl(namespace, "describe", f"pod/{_GIT_SSH_SERVER_POD}"),
            capture_output=True,
            text=True,
        )
        logs = subprocess.run(  # nosec: B603
            kubectl(namespace, "logs", f"pod/{_GIT_SSH_SERVER_POD}"),
            capture_output=True,
            text=True,
        )
        logger.error("Pod describe:\n%s", describe.stdout)
        logger.error("Pod logs:\n%s", logs.stdout)
        raise

    # Get the pod IP
    result = subprocess.run(  # nosec: B603
        kubectl(
            namespace,
            "get",
            f"pod/{_GIT_SSH_SERVER_POD}",
            "-o=jsonpath={.status.podIP}",
        ),
        capture_output=True,
        text=True,
        check=True,
    )
    pod_ip = result.stdout.strip()

    # Wait briefly for sshd to start accepting connections, then scan host keys
    host_keys = ""
    for attempt in range(5):
        result = subprocess.run(  # nosec: B603
            ["/usr/bin/ssh-keyscan", "-T", "5", pod_ip],
            capture_output=True,
            text=True,
        )
        if result.stdout.strip():
            host_keys = result.stdout
            break
        logger.info("ssh-keyscan attempt %d returned empty, retrying...", attempt + 1)
        time.sleep(3)

    if not host_keys:
        pytest.fail(f"ssh-keyscan could not retrieve host keys from {pod_ip}")

    yield {
        "ip": pod_ip,
        "private_key": private_key,
        "host_keys": host_keys,
        "repo_url": f"git@{pod_ip}:{_REPO_PATH}",
    }

    # Cleanup
    subprocess.run(  # nosec: B603
        kubectl(
            namespace,
            "delete",
            f"pod/{_GIT_SSH_SERVER_POD}",
            "--grace-period=0",
            "--force",
        ),
        capture_output=True,
    )


@pytest.fixture(scope="module")
def secret_uri(
    juju: jubilant.Juju, app: App, git_ssh_server: dict[str, str]
) -> Generator[str, None, None]:
    """Create a Juju secret holding the git-sync SSH key and yield its URI."""
    uri = juju.add_secret(
        "git-sync-ssh-key",
        {"git-sync": git_ssh_server["private_key"]},
    )
    juju.grant_secret(uri, app.name)
    yield uri


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
def test_git_sync_ssh(
    juju: jubilant.Juju,
    app: App,
    git_ssh_server: dict[str, str],
    secret_uri: str,
    ingress_address: str,
    requests_timeout: int,
):
    """Test that git-sync clones a repository over SSH and assets are available."""
    juju.config(
        app.name,
        {
            "static-assets-git-repo": git_ssh_server["repo_url"],
            "ssh-known-hosts": git_ssh_server["host_keys"],
            "ssh-key": secret_uri,
        },
    )

    juju.wait(jubilant.all_active)
    _wait_for_file_contents(
        juju, app, f"{_STATIC_ASSETS_PATH}/asset.txt", "hello from git-ssh-server"
    )

    # Check that the file is being served via the ingress
    url = f"{ingress_address}{_STATIC_ASSETS_URL_PATH}/asset.txt"
    response = requests.get(url, timeout=requests_timeout)
    assert response.status_code == 200, f"Expected 200 OK for {url!r}, got {response.status_code}"
    assert response.text.strip() == "hello from git-ssh-server", f"Unexpected content at {url!r}"


@pytest.mark.abort_on_fail
def test_git_sync_dotfiles_blocked(
    juju: jubilant.Juju,
    app: App,
    ingress_address: str,
    requests_timeout: int,
):
    """Test that Apache blocks HTTP access to dot files under /static."""
    dot_file = ".hidden"

    assert _path_exists(juju, app, f"{_STATIC_ASSETS_PATH}/{dot_file}", container="mediawiki"), (
        f"Setup: Expected {dot_file} to exist in the container for this test"
    )

    url = f"{ingress_address}{_STATIC_ASSETS_URL_PATH}/{dot_file}"
    response = requests.get(url, timeout=requests_timeout, allow_redirects=True)
    assert response.status_code == 403, (
        f"Expected 403 Forbidden for dot file {url!r}, got {response.status_code}"
    )


@pytest.mark.abort_on_fail
def test_git_sync_sparse_checkout(
    juju: jubilant.Juju,
    app: App,
):
    """Test that static-assets-git-sparse-checkout limits which files are checked out."""
    # Assert baseline that README.md is present
    assert _path_exists(juju, app, f"{_STATIC_ASSETS_PATH}/README.md"), (
        "Setup: Expected README.md to exist before applying sparse checkout"
    )

    juju.config(
        app.name,
        {"static-assets-git-sparse-checkout": "asset.txt\n"},
    )

    juju.wait(jubilant.all_active)
    # Wait until README.md is absent, meaning sparse checkout has been applied.
    _wait_for_path_exists(juju, app, f"{_STATIC_ASSETS_PATH}/README.md", exists=False)
    assert _path_exists(juju, app, f"{_STATIC_ASSETS_PATH}/asset.txt"), (
        "Expected asset.txt to exist after applying sparse checkout"
    )

    juju.config(
        app.name,
        reset="static-assets-git-sparse-checkout",
    )
    juju.wait(jubilant.all_active)


@pytest.mark.abort_on_fail
def test_git_sync_ref(
    juju: jubilant.Juju,
    app: App,
):
    """Test that setting static-assets-git-ref checks out the specified branch."""
    juju.config(
        app.name,
        {"static-assets-git-ref": "feature"},
    )

    juju.wait(jubilant.all_active)
    _wait_for_file_contents(
        juju, app, f"{_STATIC_ASSETS_PATH}/asset.txt", "hello from feature branch"
    )


@pytest.mark.abort_on_fail
def test_git_sync_disable(
    juju: jubilant.Juju,
    app: App,
):
    """Test that clearing the git repo config returns to active."""
    juju.config(
        app.name,
        reset="static-assets-git-repo",
    )

    juju.wait(jubilant.all_active)
    _wait_for_path_exists(juju, app, _STATIC_ASSETS_PATH, exists=False)
