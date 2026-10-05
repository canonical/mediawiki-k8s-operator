#!/usr/bin/env python3

# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""

import functools
import json
import logging
import re
import shlex
from pathlib import Path
from typing import Any

import jubilant
import pytest
import requests

from .types_ import App
from .utils import juju_exec, req_okay

logger = logging.getLogger(__name__)


@pytest.mark.abort_on_fail
def test_workload_version_is_set(juju: jubilant.Juju, app: App):
    """Check that the charm is the expected version."""
    status = juju.status()
    version = status.apps[app.name].version
    assert re.match(r"1\.\d{2}\.\d+$", version), (
        f"Expected workload version to match '1.XX.X' pattern, got {version}"
    )


def _shellbox_eval(juju: jubilant.Juju, app: App, statements: list[str]) -> dict[str, Any]:
    """Run a MediaWiki probe and extract its marked JSON result."""
    output = juju_exec(
        juju,
        app,
        "printf '%s\\n' "
        + " ".join(shlex.quote(statement) for statement in statements)
        + " | /usr/bin/php /var/www/html/w/maintenance/run.php eval --quiet",
    )
    payloads = [
        line.removeprefix("SHELLBOX_TEST:")
        for line in output.splitlines()
        if line.startswith("SHELLBOX_TEST:")
    ]
    assert len(payloads) == 1, output
    return json.loads(payloads[0])


def test_shellbox_command_authentication(juju: jubilant.Juju, app: App):
    """Highlight source through Shellbox and reject an invalid authentication key."""
    result = _shellbox_eval(
        juju,
        app,
        [
            r"$services = MediaWiki\MediaWikiServices::getInstance();",
            r'$html = MediaWiki\SyntaxHighlight\Pygmentize::highlight("python", '
            "'print(\"shellbox-highlight-test\")', []);",
            "$config = $services->getMainConfig();",
            r"$clients = new MediaWiki\Shell\ShellboxClientFactory("
            '$services->getHttpRequestFactory(), $config->get("ShellboxUrls"), '
            'str_repeat("0", 128));',
            '$limits = ["walltime" => 30, "time" => 30, "memory" => 262144, "filesize" => 10240];',
            r"$factory = new MediaWiki\Shell\CommandFactory($clients, $limits, false, false);",
            '$rejected = false; try { $factory->createBoxed("syntaxhighlight")'
            '->routeName("syntaxhighlight-pygments")->params("/usr/bin/pygmentize", "-V")'
            "->execute(); } catch (Shellbox\\ShellboxError $error) { $rejected = true; }",
            'echo "\\nSHELLBOX_TEST:" . json_encode(["html" => $html, "remote" => '
            '$services->getShellboxClientFactory()->isEnabled("syntaxhighlight"), '
            '"badKeyRejected" => $rejected]) . "\\n";',
        ],
    )
    assert result["remote"] is True
    assert '<span class="nb">print</span>' in result["html"]
    assert '<span class="s2">' in result["html"]
    assert "shellbox-highlight-test" in result["html"]
    assert result["badKeyRejected"] is True


def _shellbox_command_probe(juju: jubilant.Juju, app: App, probe: str) -> dict[str, Any]:
    """Run a Python security probe as a real Shellbox command."""
    result = _shellbox_eval(
        juju,
        app,
        [
            r"$services = MediaWiki\MediaWikiServices::getInstance();",
            '$probe = "";',
            *[f"$probe .= {json.dumps(line)};" for line in probe.splitlines(keepends=True)],
            '$command = $services->getShellCommandFactory()->createBoxed("syntaxhighlight")'
            '->routeName("shellbox-test-runtime-user")'
            '->params("/usr/bin/python3", "-c", $probe);',
            "$result = $command->execute();",
            'echo "\\nSHELLBOX_TEST:" . json_encode(["exit" => $result->getExitCode(), '
            '"probe" => json_decode($result->getStdout(), true)]) . "\\n";',
        ],
    )
    assert result["exit"] == 0, result
    assert isinstance(result["probe"], dict), result
    return result["probe"]


def test_shellbox_command_isolation(juju: jubilant.Juju, app: App):
    """Keep unprivileged commands separate from FastCGI and the signing key."""
    probe = (
        "import json, os, socket\n"
        "from pathlib import Path\n"
        'path = "/run/php/shellbox.sock"\n'
        'config = "/srv/shellbox/config/config.json"\n'
        "with open(config) as config_file:\n"
        "    settings = json.load(config_file)\n"
        "fpm_count = 0\n"
        "fpm_environ_readable = False\n"
        'for process in Path("/proc").iterdir():\n'
        "    if not process.name.isdigit():\n"
        "        continue\n"
        "    try:\n"
        '        if not (process / "comm").read_text().startswith("php-fpm"):\n'
        "            continue\n"
        "        fpm_count += 1\n"
        "        try:\n"
        '            with (process / "environ").open("rb") as environment:\n'
        "                environment.read(1)\n"
        "            fpm_environ_readable = True\n"
        "        except PermissionError:\n"
        "            pass\n"
        "    except FileNotFoundError:\n"
        "        continue\n"
        "client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        "try:\n"
        "    client.connect(path)\n"
        "    socket_errno = 0\n"
        "except OSError as error:\n"
        "    socket_errno = error.errno\n"
        "finally:\n"
        "    client.close()\n"
        'print(json.dumps({"uid": os.geteuid(), "gid": os.getegid(), '
        '"config_writable": os.access(config, os.W_OK), '
        '"config_contains_key": "secretKey" in settings, '
        '"env_contains_key": bool(os.environ.get("SHELLBOX_SECRET_KEY")), '
        '"fpm_count": fpm_count, '
        '"fpm_environ_readable": fpm_environ_readable, '
        '"socket_errno": socket_errno, '
        '"key_file_readable": os.access("/etc/shellbox/key.conf", os.R_OK)}))\n'
    )
    result = _shellbox_command_probe(juju, app, probe)
    assert result["uid"] != 0 and result["gid"] != 0, result
    assert result["config_writable"] is False, result
    assert result["config_contains_key"] is False, result
    assert result["env_contains_key"] is False, result
    assert result["fpm_count"] >= 2, result
    assert result["fpm_environ_readable"] is False, result
    assert result["socket_errno"] == 13, result
    assert result["key_file_readable"] is False, result


def test_shellbox_apache_status_isolation(juju: jubilant.Juju, app: App):
    """Expose only aggregate Apache counters on the dedicated status listeners."""
    probe = (
        "import json, urllib.error, urllib.request\n"
        "def status_response(port, query):\n"
        "    try:\n"
        "        response = urllib.request.urlopen(\n"
        '            f"http://127.0.0.1:{port}/server-status{query}", timeout=5)\n'
        "    except urllib.error.HTTPError as error:\n"
        "        response = error\n"
        "    with response:\n"
        "        body = response.read()\n"
        '    return response.code, b"ServerVersion:" in body or b"Apache Server Status for" in body\n'
        "responses = [status_response(port, query) for port, query in\n"
        '    ((8090, "?auto"), (8091, "?auto"), (8090, ""), (8091, ""), '
        '(8080, "?auto"), (80, "?auto"))]\n'
        'print(json.dumps({"status_codes": [response[0] for response in responses], '
        '"status_pages": [response[1] for response in responses]}))\n'
    )
    result = _shellbox_command_probe(juju, app, probe)
    assert result["status_codes"][:4] == [200, 200, 403, 403], result
    assert all(code in (200, 403, 404) for code in result["status_codes"][4:]), result
    assert result["status_pages"] == [True, True, False, False, False, False], result


def test_shellbox_pebble_plan_hides_key(juju: jubilant.Juju, app: App):
    """Deny plan access or expose a valid plan without the Shellbox signing key."""
    probe = (
        "import http.client, json, socket\n"
        "plan_status = None\n"
        "access_denied = False\n"
        'body = b""\n'
        "try:\n"
        "    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:\n"
        "        client.settimeout(5)\n"
        '        client.connect("/charm/container/pebble.socket")\n'
        '        client.sendall(b"GET /v1/plan?format=yaml HTTP/1.1\\r\\nHost: localhost\\r\\n'
        'Connection: close\\r\\n\\r\\n")\n'
        "        response = http.client.HTTPResponse(client)\n"
        "        response.begin()\n"
        "        plan_status = response.status\n"
        "        body = response.read()\n"
        "except PermissionError:\n"
        "    access_denied = True\n"
        'plan = json.loads(body).get("result") if plan_status == 200 else None\n'
        "valid_plan = isinstance(plan, str) and bool(plan)\n"
        'contains_key = b"SHELLBOX_SECRET_KEY" in body or '
        '(valid_plan and "SHELLBOX_SECRET_KEY" in plan)\n'
        'print(json.dumps({"plan_status": plan_status, "access_denied": access_denied, '
        '"valid_plan": valid_plan, "contains_key": bool(contains_key)}))\n'
    )
    result = _shellbox_command_probe(juju, app, probe)
    assert result["access_denied"] or result["plan_status"] in (200, 401, 403), result
    if result["plan_status"] == 200:
        assert result["valid_plan"] is True, result
    assert result["contains_key"] is False, result


@pytest.mark.abort_on_fail
def test_tls_certificate_lifecycle(
    juju: jubilant.Juju,
    app: App,
    ssc: App,
    traefik: App,
    ingress_address: str,
    requests_timeout: int,
):
    """Check TLS enablement, HTTP fallback, and re-enablement for later tests."""

    def _tls_material_matches(*, ready: bool) -> bool:
        """Return whether Apache TLS material matches the expected state."""
        predicate = (
            "test -L /etc/apache2/sites-enabled/mediawiki-tls.conf "
            "&& test -s /etc/mediawiki/tls/certificate.pem "
            "&& test -s /etc/mediawiki/tls/private-key.pem"
            if ready
            else "test ! -e /etc/apache2/sites-enabled/mediawiki-tls.conf "
            "&& test ! -e /etc/mediawiki/tls/certificate.pem "
            "&& test ! -e /etc/mediawiki/tls/private-key.pem"
        )
        return juju_exec(juju, app, f"{predicate} && echo matched || true").strip() == "matched"

    juju.integrate(f"{traefik.name}:receive-ca-cert", f"{ssc.name}:send-ca-cert")
    juju.integrate(f"{app.name}:certificates", f"{ssc.name}:certificates")
    juju.wait(
        lambda status: (
            jubilant.all_active(status)
            and "certificates" in status.apps[app.name].relations
            and _tls_material_matches(ready=True)
            and req_okay(ingress_address, requests_timeout)
        ),
        error=jubilant.any_error,
    )

    assert "Syntax OK" in juju_exec(juju, app, "apache2ctl configtest 2>&1")
    assert (
        juju_exec(
            juju,
            app,
            "test -L /etc/apache2/sites-enabled/mediawiki-tls.conf && echo enabled",
        ).strip()
        == "enabled"
    )
    assert (
        juju_exec(
            juju,
            app,
            "test -s /etc/mediawiki/tls/certificate.pem "
            "&& test -s /etc/mediawiki/tls/private-key.pem && echo present",
        ).strip()
        == "present"
    )

    juju.remove_relation(f"{app.name}:certificates", f"{ssc.name}:certificates")
    juju.wait(
        lambda status: (
            jubilant.all_active(status)
            and req_okay(ingress_address, requests_timeout)
            and "certificates" not in status.apps[app.name].relations
            and _tls_material_matches(ready=False)
        ),
        error=jubilant.any_error,
    )
    assert (
        juju_exec(
            juju,
            app,
            "test ! -e /etc/apache2/sites-enabled/mediawiki-tls.conf "
            "&& test ! -e /etc/mediawiki/tls/certificate.pem "
            "&& test ! -e /etc/mediawiki/tls/private-key.pem && echo removed",
        ).strip()
        == "removed"
    )

    juju.integrate(f"{app.name}:certificates", f"{ssc.name}:certificates")
    juju.wait(
        lambda status: (
            jubilant.all_active(status)
            and "certificates" in status.apps[app.name].relations
            and _tls_material_matches(ready=True)
            and req_okay(ingress_address, requests_timeout)
        ),
        error=jubilant.any_error,
    )


@pytest.mark.abort_on_fail
def test_valkey_tls_certificate_transfer(
    juju: jubilant.Juju,
    app: App,
    ssc: App,
):
    """Check MediaWiki-to-Valkey TLS with and without certificate transfer."""
    ca_bundle_path = "/usr/local/share/ca-certificates/certificate-transfer-ca.crt"

    def _ca_bundle() -> str:
        """Return the charm-owned CA bundle, or an empty string if it is absent."""
        return juju_exec(
            juju,
            app,
            f"test -f {ca_bundle_path} && cat {ca_bundle_path} || true",
        )

    def _cache_roundtrip() -> str:
        """Perform a MediaWiki cache read/write against the configured Valkey."""
        return juju_exec(
            juju,
            app,
            "printf '%s\\n' '"
            '$cache = ObjectCache::getInstance( "redis" ); '
            '$key = "certificate-transfer-test"; '
            '$cache->set( $key, "certificate-transfer-ok", 60 ); '
            "echo $cache->get( $key );' | "
            "/usr/bin/php /var/www/html/w/maintenance/run.php eval",
        )

    def _cache_uses_tls() -> bool:
        """Return whether MediaWiki's configured cache endpoint uses TLS."""
        return "tls://" in juju_exec(
            juju,
            app,
            "cat /etc/mediawiki/LateSettings.php",
        )

    def _cache_tls_protocol() -> str:
        """Return the TLS protocol or handshake error for the configured Valkey endpoint."""
        return juju_exec(
            juju,
            app,
            "printf '%s\\n' '"
            '$server = $wgObjectCaches["redis"]["servers"][0]; '
            '$context = stream_context_create( [ "ssl" => [ "verify_peer" => true, '
            '"verify_peer_name" => false ] ] ); '
            "$socket = @stream_socket_client( $server, $errno, $errstr, 10, "
            "STREAM_CLIENT_CONNECT, $context ); "
            'if ( !$socket ) { echo "ERROR endpoint=$server errno=$errno error=$errstr"; exit; } '
            "$metadata = stream_get_meta_data( $socket ); "
            'echo $metadata["crypto"]["protocol"];'
            "' | /usr/bin/php /var/www/html/w/maintenance/run.php eval",
        ).strip()

    def _cache_tls_ready(status: jubilant.Status) -> bool:
        """Return whether the cache uses TLS and a TLS handshake succeeds."""
        if not jubilant.all_active(status) or not _cache_uses_tls():
            return False
        protocol = _cache_tls_protocol()
        if not protocol.startswith("TLSv"):
            logger.info("Valkey TLS probe result: %s", protocol)
            return False
        return True

    juju.wait(
        _cache_tls_ready,
        error=jubilant.any_error,
    )
    bundle_without_certificate_transfer = _ca_bundle()
    assert "certificate-transfer-ok" in _cache_roundtrip()

    juju.integrate(f"{app.name}:receive-ca-cert", f"{ssc.name}:send-ca-cert")
    juju.wait(
        lambda status: (
            jubilant.all_agents_idle(status)
            and _cache_tls_ready(status)
            and "receive-ca-cert" in status.apps[app.name].relations
            and _ca_bundle() == bundle_without_certificate_transfer
        ),
        error=jubilant.any_error,
    )
    assert "certificate-transfer-ok" in _cache_roundtrip()


@pytest.mark.abort_on_fail
def test_ssh_key_secret(
    juju: jubilant.Juju, app: App, app_config: dict[str, Any], ssh_key_secret: str
):
    """Check that the charm behaves correct regarding the ssh_key Juju secret.

    Note that this test does not attempt to utilize the SSH key as the passed key is not expected to be
    authorized anywhere.
    """
    initial_secret_content = juju.show_secret(ssh_key_secret, reveal=True).content

    app_config["ssh-key"] = ssh_key_secret
    juju.config(app.name, app_config)
    juju.wait(jubilant.all_active, successes=5)

    # Block due to empty mediawiki SSH key
    juju.update_secret(ssh_key_secret, {"mediawiki": ""})
    juju.wait(lambda status: jubilant.all_blocked(status, app.name))

    # Reset secret
    juju.update_secret(ssh_key_secret, initial_secret_content)
    juju.wait(jubilant.all_active)

    # Block due to no valid keys in secret
    juju.update_secret(ssh_key_secret, {"invalid-field": "value"})
    juju.wait(lambda status: jubilant.all_blocked(status, app.name))

    app_config.pop("ssh-key")
    juju.config(app.name, app_config, reset="ssh-key")
    juju.wait(jubilant.all_active)


@pytest.mark.abort_on_fail
def test_add_extensions(
    juju: jubilant.Juju,
    app: App,
    app_config: dict[str, Any],
    ingress_address: str,
    requests_timeout: int,
):
    """Check that the charm can have extensions added after deployment.

    Add extensions by editing the composer config, and then running a database update.
    """
    is_reachable = functools.partial(req_okay, address=ingress_address, timeout=requests_timeout)

    juju.wait(jubilant.all_active)
    assert is_reachable(), "MediaWiki not responding at ingress before adding extensions"

    composer = Path(__file__).parent / "test_data" / "composer.json"
    app_config["composer"] = composer.read_text()
    app_config["local-settings"] += "wfLoadExtension( 'CheckUser' );\n"
    app_config["local-settings"] += "wfLoadExtension( 'Linter' );\n"
    app_config["local-settings"] += "wfLoadExtension( 'DiscussionTools' );\n"
    app_config["local-settings"] += "wfLoadExtension( 'Echo' );\n"
    app_config["local-settings"] += "wfLoadExtension( 'Thanks' );\n"
    app_config["local-settings"] += "wfLoadExtension( 'UserMerge' );\n"
    app_config["local-settings"] += "wfLoadExtension( 'PageTriage' );\n"
    app_config["local-settings"] += "wfLoadExtension( 'Mermaid' );\n"

    juju.config(app.name, app_config)
    juju.wait(jubilant.all_active, timeout=5 * 60)

    update_database_action = juju.run(f"{app.name}/leader", "update-database")
    assert update_database_action.status == "completed"
    # The DB update completes asynchronously to the action, so we need to be certain that no other actions are still running
    juju.wait(jubilant.all_active, successes=5)

    assert is_reachable(), "MediaWiki not responding at ingress after adding extensions"

    loaded_extensions = requests.get(
        f"{ingress_address}/w/api.php?action=query&meta=siteinfo&siprop=extensions&format=json&formatversion=2",
        timeout=requests_timeout,
    ).json()

    loaded_extensions = {ext["name"] for ext in loaded_extensions["query"]["extensions"]}
    assert "UserMerge" in loaded_extensions, "UserMerge extension not loaded"
    assert "PageTriage" in loaded_extensions, "PageTriage extension not loaded"
    assert "Mermaid" in loaded_extensions, "Mermaid extension not loaded"


@pytest.mark.abort_on_fail
def test_rotate_mediawiki_secrets_action(juju: jubilant.Juju, app: App):
    """Check that the rotate-mediawiki-secrets action works as expected."""
    rotate_action = juju.run(f"{app.name}/leader", "rotate-mediawiki-secrets")
    assert rotate_action.status == "completed", (
        f"Action failed with message: {rotate_action.message}"
    )

    juju.wait(jubilant.all_active)


@pytest.mark.abort_on_fail
def test_create_and_promote_action(juju: jubilant.Juju, app: App):
    """Check that the create-and-promote action works as expected."""
    create_action = juju.run(
        f"{app.name}/leader",
        "create-and-promote",
        {"username": "alice", "bureaucrat": True, "generate-password": True},
    )
    assert create_action.status == "completed"
    assert create_action.results["username"] == "alice"
    assert len(create_action.results["password"]) >= 64, (
        "Expected generated password to be at least 64 characters long"
    )  # secrets.token_urlsafe(64) averages 1.3 characters per byte to 83

    # Re-running without force should fail because the user already exists, even
    # when a password would be generated.
    with pytest.raises(jubilant.TaskError):
        juju.run(
            f"{app.name}/leader",
            "create-and-promote",
            {"username": "alice", "bureaucrat": True, "generate-password": True},
        )

    # Creating a user without generating a password and without force should fail
    # validation.
    with pytest.raises(jubilant.TaskError):
        juju.run(
            f"{app.name}/leader",
            "create-and-promote",
            {"username": "alice", "generate-password": False},
        )

    # Promoting an existing user with force and no password generation should
    # succeed and not return a password.
    promote_action = juju.run(
        f"{app.name}/leader",
        "create-and-promote",
        {"username": "alice", "generate-password": False, "force": True, "sysop": True},
    )
    assert promote_action.status == "completed"
    assert promote_action.results["username"] == "alice"
    assert "password" not in promote_action.results, (
        "Did not expect a password to be returned when generation is disabled"
    )

    juju.wait(jubilant.all_active)


@pytest.mark.abort_on_fail
def test_force_reconciliation_action(juju: jubilant.Juju, app: App):
    """Check that the force-reconciliation action works on the leader and on all units."""
    # Single unit (leader)
    action = juju.run(f"{app.name}/leader", "force-reconciliation", wait=5 * 60)
    assert action.status == "completed", f"force-reconciliation on leader failed: {action.message}"
    juju.wait(jubilant.all_active)

    # All units via peer coordination
    all_units_action = juju.run(f"{app.name}/leader", "force-reconciliation", {"all-units": True})
    assert all_units_action.status == "completed", (
        f"force-reconciliation all-units failed: {all_units_action.message}"
    )
    # The update completes asynchronously via peer relation coordination
    juju.wait(jubilant.all_active, successes=5, timeout=8 * 60)


@pytest.mark.abort_on_fail
def test_run_maintenance_script_action(juju: jubilant.Juju, app: App):
    """Check that the action forwards arguments and rejects sensitive scripts."""
    action = juju.run(
        f"{app.name}/leader",
        "run-maintenance-script",
        {
            "script": "resetPageRandom",
            "args": "--from 20000101000000 --to 21000101000000 --dry",
        },
    )
    assert action.status == "completed", f"Action failed: {action.message}"
    assert "Resetting page_random column" in action.results.get("output", ""), (
        "Expected resetPageRandom output; got: " + action.results.get("output", "")
    )

    # Configuration readers and charm-managed scripts should be rejected.
    with pytest.raises(jubilant.TaskError):
        juju.run(f"{app.name}/leader", "run-maintenance-script", {"script": "getConfiguration"})
    with pytest.raises(jubilant.TaskError):
        juju.run(f"{app.name}/leader", "run-maintenance-script", {"script": "install"})


@pytest.mark.abort_on_fail
def test_relations(
    juju: jubilant.Juju,
    app: App,
    db: App,
    traefik: App,
    valkey: App,
    ingress_address: str,
    requests_timeout: int,
):
    """Check that the charm behaves correctly when certain relations are removed."""
    is_reachable = functools.partial(req_okay, address=ingress_address, timeout=requests_timeout)

    juju.wait(jubilant.all_active)
    assert is_reachable(), (
        f"MediaWiki not responding at {ingress_address} before removing relations"
    )

    # Remove traefik relation and check that the charm remains active, but the ingress address is no longer responsive
    juju.remove_relation(app.name, traefik.name)
    juju.wait(
        lambda status: (
            jubilant.all_active(status)
            and not is_reachable()
            and "traefik-route" not in status.apps[app.name].relations
        )
    )

    juju.integrate(app.name, traefik.name)
    juju.wait(
        lambda status: (
            jubilant.all_active(status)
            and is_reachable()
            and "traefik-route" in status.apps[app.name].relations
        )
    )

    # Removing database blocks and stops responsiveness entirely
    juju.remove_relation(app.name, db.name)
    juju.wait(lambda status: status.apps[app.name].is_blocked and not is_reachable())
    juju.wait(
        lambda status: (
            jubilant.all_active(status, db.name)
            and "database" not in status.apps[app.name].relations
        )
    )

    juju.integrate(app.name, db.name)
    juju.wait(
        lambda status: (
            jubilant.all_active(status)
            and is_reachable()
            and "database" in status.apps[app.name].relations
        )
    )

    # Removing Valkey does not block
    juju.remove_relation(app.name, valkey.name)
    juju.wait(
        lambda status: (
            jubilant.all_active(status)
            and is_reachable()
            and "valkey" not in status.apps[app.name].relations
        ),
        successes=5,
    )

    juju.integrate(f"{app.name}:valkey", f"{valkey.name}:valkey-client")
    juju.wait(
        lambda status: (
            jubilant.all_active(status)
            and is_reachable()
            and "valkey" in status.apps[app.name].relations
        ),
        successes=5,
    )
