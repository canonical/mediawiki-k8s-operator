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


def _check_command_output(
    juju: jubilant.Juju, app: App, command: str, expected_output: str
) -> bool:
    """Check if *command* output in the mediawiki container contains *expected_output*."""
    try:
        result = juju.ssh(
            f"{app.name}/0",
            command,
            container="mediawiki",
        )
        return expected_output in result
    except Exception:
        return False


def _wait_for_command_output(
    juju: jubilant.Juju, app: App, command: str, expected_output: str
) -> None:
    """Wait until *command* output in the mediawiki container contains *expected_output*."""
    juju.wait(
        lambda _: _check_command_output(juju, app, command, expected_output),
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
        subprocess.run(  # nosec: B603, B607
            ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-q"],
            check=True,
        )
        private_key = key_path.read_text()
        public_key = key_path.with_suffix(".pub").read_text().strip()

    # Load the manifest template and substitute placeholders
    manifest_template = (_TEST_DATA / "git-ssh-server-pod.yaml").read_text()
    manifest = manifest_template.replace("__GIT_SSH_PUBLIC_KEY__", public_key)

    # Apply the pod manifest
    subprocess.run(  # nosec: B603, B607
        kubectl(namespace, "apply", "-f", "-"),
        input=manifest,
        text=True,
        check=True,
        capture_output=True,
    )

    # Wait for the pod to be ready
    try:
        subprocess.run(  # nosec: B603, B607
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
        describe = subprocess.run(  # nosec: B603, B607
            kubectl(namespace, "describe", f"pod/{_GIT_SSH_SERVER_POD}"),
            capture_output=True,
            text=True,
        )
        logs = subprocess.run(  # nosec: B603, B607
            kubectl(namespace, "logs", f"pod/{_GIT_SSH_SERVER_POD}"),
            capture_output=True,
            text=True,
        )
        logger.error("Pod describe:\n%s", describe.stdout)
        logger.error("Pod logs:\n%s", logs.stdout)
        raise

    # Get the pod IP
    result = subprocess.run(  # nosec: B603, B607
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
        result = subprocess.run(  # nosec: B603, B607
            ["ssh-keyscan", "-T", "5", pod_ip],
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
    subprocess.run(  # nosec: B603, B607
        kubectl(
            namespace,
            "delete",
            f"pod/{_GIT_SSH_SERVER_POD}",
            "--grace-period=0",
            "--force",
        ),
        capture_output=True,
    )


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
    ingress_address: str,
    requests_timeout: int,
):
    """Test that git-sync clones a repository over SSH and assets are available."""
    secret_uri = juju.add_secret(
        "git-sync-ssh-key",
        {"git-sync": git_ssh_server["private_key"]},
    )
    juju.grant_secret(secret_uri, app.name)

    juju.config(
        app.name,
        {
            "static-assets-git-repo": git_ssh_server["repo_url"],
            "ssh-known-hosts": git_ssh_server["host_keys"],
            "ssh-key": secret_uri,
        },
    )

    juju.wait(jubilant.all_active)
    _wait_for_command_output(
        juju, app, f"cat {_STATIC_ASSETS_PATH}/asset.txt", "hello from git-ssh-server"
    )

    # Check that the file is being served via the ingress
    url = f"{ingress_address}{_STATIC_ASSETS_URL_PATH}/asset.txt"
    response = requests.get(url, timeout=requests_timeout)  # nosec: B113
    assert response.status_code == 200, f"Expected 200 OK for {url!r}, got {response.status_code}"
    assert response.text.strip() == "hello from git-ssh-server", f"Unexpected content at {url!r}"


@pytest.mark.abort_on_fail
def test_git_sync_dotfiles_blocked(
    ingress_address: str,
    requests_timeout: int,
):
    """Test that Apache blocks HTTP access to dot files under /static."""
    dot_paths = [".hidden", ".git/config"]
    for path in dot_paths:
        url = f"{ingress_address}{_STATIC_ASSETS_URL_PATH}/{path}"
        response = requests.get(url, timeout=requests_timeout, allow_redirects=True)  # nosec: B113
        assert response.status_code == 403, (
            f"Expected 403 Forbidden for dot file {url!r}, got {response.status_code}"
        )


@pytest.mark.abort_on_fail
def test_git_sync_ref(
    juju: jubilant.Juju,
    app: App,
    git_ssh_server: dict[str, str],
    secret_uri: str,
):
    """Test that setting static-assets-git-ref checks out the specified branch."""
    juju.config(
        app.name,
        {
            "static-assets-git-repo": git_ssh_server["repo_url"],
            "ssh-known-hosts": git_ssh_server["host_keys"],
            "ssh-key": secret_uri,
            "static-assets-git-ref": "feature",
        },
    )

    juju.wait(jubilant.all_active)
    _wait_for_command_output(
        juju, app, f"cat {_STATIC_ASSETS_PATH}/asset.txt", "hello from feature branch"
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
    _wait_for_command_output(juju, app, f"file {_STATIC_ASSETS_PATH}", "file: not found")
