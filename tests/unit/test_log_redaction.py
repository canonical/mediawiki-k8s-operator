# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Test masking of final log output, rather than unformatted log records."""

import dataclasses
import io
import json
import logging
import sys
import traceback
from unittest.mock import Mock

import ops
import pytest
from charmlibs.interfaces.tls_certificates import PrivateKey
from ops import testing
from ops.log import JujuLogHandler

import log_redaction
from charm import Charm
from container import ContainerService
from exceptions import MediaWikiBlockedStatusException, MediaWikiInstallError
from mediawiki import MediaWikiSecrets
from types_ import CommandExecResult
from utils import escape_php_string


@pytest.fixture
def log_sink():
    """Capture output from a real Juju handler and restore global logging state."""
    root = logging.getLogger()
    original_level = root.level
    original_formatters = {handler: handler.formatter for handler in root.handlers}
    backend = Mock(spec=["juju_log"])
    handler = JujuLogHandler(backend)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    log_redaction.install()
    yield backend
    root.removeHandler(handler)
    root.setLevel(original_level)
    for existing, formatter in original_formatters.items():
        existing.setFormatter(formatter)
    log_redaction.install()
    for existing, formatter in original_formatters.items():
        existing.setFormatter(formatter)


def _output(sink: Mock) -> str:
    return "\n".join(call.args[1] for call in sink.juju_log.call_args_list)


class TestSecretRedactor:
    @pytest.mark.parametrize(
        "render",
        [
            str,
            repr,
            lambda value: repr({"password": value}),
            lambda value: repr(value.encode()),
            json.dumps,
            escape_php_string,
        ],
    )
    def test_rendered_representations(self, log_sink, render):
        secret = "test-'\"\\credential\nwith-\u00e9"  # nosec: B105
        log_redaction.register_secrets(secret)
        logging.getLogger("dependency.child").warning("Credentials: %s", render(secret))

        output = _output(log_sink)
        assert render(secret) not in output
        assert "[REDACTED]" in output
        assert "WARNING dependency.child: Credentials:" in output

    def test_literal_overlapping_and_short_values(self):
        redactor = log_redaction.SecretRedactor()
        redactor.register("", None, "abc", "abcdef", ".*[a]", "x", "REDACTED")

        assert redactor.redact("abcdef abc .*[a] x safe") == (
            "[REDACTED] [REDACTED] [REDACTED] [REDACTED] safe"
        )
        redactor.register("next-secret")
        assert redactor.redact("next-secret") == "[REDACTED]"

    def test_private_key_lines(self, log_sink):
        key = "-----BEGIN PRIVATE KEY-----\nfirst-payload\nsecond-payload\n-----END PRIVATE KEY-----\n"
        log_redaction.register_secrets(key)
        logger = logging.getLogger("keys")
        logger.error("%s", key)
        logger.error("%r", key)
        for line in key.splitlines():
            logger.error("%s", line)

        output = _output(log_sink)
        assert "first-payload" not in output
        assert "second-payload" not in output
        assert "BEGIN PRIVATE KEY" in output

    def test_registration_recompiles_and_does_not_change_inputs(self, log_sink):
        payload = {"password": "first-secret"}  # nosec: B105
        log_redaction.register_secrets(payload["password"])
        logging.getLogger("test").info("%r", payload)
        log_redaction.register_secrets("second-secret")
        logging.getLogger("test").info("second-secret")

        assert payload == {"password": "first-secret"}  # nosec: B105
        assert "first-secret" not in _output(log_sink)
        assert "second-secret" not in _output(log_sink)

    def test_install_is_idempotent_and_starts_fresh(self, log_sink):
        log_redaction.register_secrets("previous-invocation")
        log_redaction.install()
        log_redaction.install()
        logging.getLogger("test").warning("previous-invocation is ordinary text now")

        assert _output(log_sink) == ("WARNING test: previous-invocation is ordinary text now")

    def test_install_preserves_exception_hook_without_emitting_output(self, log_sink, monkeypatch):
        hook = Mock()
        monkeypatch.setattr(sys, "excepthook", hook)

        log_redaction.install()
        log_redaction.install()

        assert sys.excepthook is hook
        hook.assert_not_called()
        log_sink.juju_log.assert_not_called()

    def test_empty_registry_preserves_format_and_level(self, log_sink):
        logging.getLogger("test").error("Safe diagnostic: %s", 42)

        log_sink.juju_log.assert_called_once_with("ERROR", "ERROR test: Safe diagnostic: 42")

    def test_debug_stream_and_warning_logger(self, log_sink):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            log_redaction.install()
            log_redaction.register_secrets("warning-credential")
            logging.getLogger("py.warnings").warning("Warning: warning-credential")

            assert stream.getvalue() == "Warning: [REDACTED]\n"
            assert "warning-credential" not in _output(log_sink)
        finally:
            root.removeHandler(handler)


class TestTracebackMasking:
    def test_exception_chain_and_preformatted_traceback(self, log_sink):
        log_redaction.register_secrets("inner-secret", "outer-secret")
        logger = logging.getLogger("exceptions")
        try:
            try:
                raise ValueError("inner-secret")
            except ValueError as error:
                raise RuntimeError("outer-secret") from error
        except RuntimeError:
            logger.exception("Operation failed")
            logger.error("%s", traceback.format_exc())

        output = _output(log_sink)
        assert "inner-secret" not in output
        assert "outer-secret" not in output
        assert "Traceback (most recent call last)" in output
        assert "ValueError: [REDACTED]" in output
        assert "RuntimeError: [REDACTED]" in output
        assert "direct cause" in output

    def test_explicit_exception_cached_text_stack_and_multiple_handlers(self, log_sink):
        backend = Mock(spec=["juju_log"])
        second_handler = JujuLogHandler(backend)
        root = logging.getLogger()
        root.addHandler(second_handler)
        try:
            log_redaction.install()
            log_redaction.register_secrets("cached-secret", "stack-secret")
            error = ValueError("cached-secret")
            record = logging.LogRecord(
                "exceptions",
                logging.ERROR,
                __file__,
                1,
                "Failed %s",
                ("cached-secret",),
                (ValueError, error, None),
                sinfo="stack-secret",
            )
            logging.Formatter().format(record)
            root.handle(record)

            for sink in (log_sink, backend):
                output = _output(sink)
                assert "cached-secret" not in output
                assert "stack-secret" not in output
                assert "ValueError: [REDACTED]" in output
            assert record.args == ("cached-secret",)
            assert str(error) == "cached-secret"
        finally:
            root.removeHandler(second_handler)

    def test_ops_uncaught_exception_python_record(self, log_sink, monkeypatch, mocker):
        import ops.log

        monkeypatch.setattr(sys, "excepthook", sys.excepthook)
        import warnings

        monkeypatch.setattr(warnings, "showwarning", warnings.showwarning)
        # Ops security audit events use hook tools directly, not Python logging.
        mocker.patch("ops.log._log_security_event", autospec=True)
        root = logging.getLogger()
        handlers = list(root.handlers)
        try:
            ops.log.setup_root_logging(log_sink)
            log_redaction.install()
            log_redaction.register_secrets("uncaught-secret")
            error = ValueError("uncaught-secret")
            sys.excepthook(ValueError, error, None)

            assert "uncaught-secret" not in _output(log_sink)
            assert "ValueError: [REDACTED]" in _output(log_sink)
        finally:
            for handler in list(root.handlers):
                if handler not in handlers:
                    root.removeHandler(handler)


class TestCredentialLifecycle:
    def test_generated_peer_secrets_are_registered(self, log_sink):
        secrets = MediaWikiSecrets.generate()
        logging.getLogger("peer").error("%r", secrets)

        output = _output(log_sink)
        for value in secrets.to_juju_secret().values():
            assert value not in output
        assert output.count("[REDACTED]") == 4

    @pytest.mark.parametrize(
        ("endpoint", "data", "credential"),
        [
            ("database", {"password": "database-credential"}, "database-credential"),  # nosec: B105
            ("smtp", {"password": "smtp-credential"}, "smtp-credential"),  # nosec: B105
            ("oauth", {"client_secret": "oauth-credential"}, "oauth-credential"),  # nosec: B105
            ("s3-parameters", {"access-key": "s3-credential"}, "s3-credential"),
            (
                "valkey",
                {"version": '"v1"', "requests": '[{"password":"valkey-credential"}]'},
                "valkey-credential",
            ),
        ],
    )
    def test_seed_before_standalone_action(
        self, ctx, base_state, log_sink, mocker, endpoint, data, credential
    ):
        relation = testing.Relation(endpoint=endpoint, remote_app_data=data)
        state = dataclasses.replace(base_state, relations=[relation])

        def fail(_peers):
            logging.getLogger("dependency").exception("Failed with %s", credential)

        mocker.patch(
            "mediawiki_peers.MediaWikiPeers.rotate_secrets", autospec=True, side_effect=fail
        )
        ctx.run(ctx.on.action("rotate-mediawiki-secrets"), state)

        output = _output(log_sink)
        assert credential not in output
        assert "Failed with [REDACTED]" in output

    @pytest.mark.parametrize(
        ("endpoint", "reference", "field"),
        [
            ("database", "secret-user", "password"),
            ("smtp", "password_id", "password"),
            ("oauth", "client_secret_id", "secret"),
            ("valkey", "secret-user", "password"),
        ],
    )
    def test_seed_referenced_secret(
        self, ctx, base_state, log_sink, mocker, endpoint, reference, field
    ):
        secret = testing.Secret({field: "referenced-credential"})
        relation = testing.Relation(endpoint=endpoint, remote_app_data={reference: secret.id})
        state = dataclasses.replace(
            base_state, secrets=[*base_state.secrets, secret], relations=[relation]
        )
        mocker.patch(
            "mediawiki_peers.MediaWikiPeers.rotate_secrets",
            autospec=True,
            side_effect=lambda _: logging.getLogger("test").error("referenced-credential"),
        )

        ctx.run(ctx.on.action("rotate-mediawiki-secrets"), state)

        assert "referenced-credential" not in _output(log_sink)
        assert "[REDACTED]" in _output(log_sink)

    def test_action_password_is_masked_but_result_is_unchanged(
        self, ctx, base_state, log_sink, mocker
    ):
        mocker.patch("mediawiki._core.secrets.token_urlsafe", return_value="generated-credential")
        mocker.patch(
            "mediawiki._core.MediaWiki._run_maintenance_script",
            autospec=True,
            return_value=CommandExecResult(0, "generated-credential", ""),
        )

        ctx.run(
            ctx.on.action(
                "create-and-promote",
                params={
                    "username": "admin",
                    "generate-password": True,
                },
            ),
            base_state,
        )

        assert ctx.action_results["password"] == "generated-credential"
        assert "generated-credential" not in _output(log_sink)
        assert "User creation output:\n[REDACTED]" in _output(log_sink)

    def test_failed_command_output_is_masked(self, log_sink):
        log_redaction.register_secrets("command-credential")
        result = CommandExecResult(1, "command-credential", "command-credential")

        with pytest.raises(MediaWikiInstallError):
            result.raise_for_status("Running command", MediaWikiInstallError)

        assert "command-credential" not in _output(log_sink)
        assert _output(log_sink).count("[REDACTED]") == 2

    @pytest.mark.parametrize("exit_code", [0, 1])
    def test_script_action_receives_masked_command_output(
        self, ctx, base_state, log_sink, mocker, exit_code
    ):
        content = base_state.get_secret(label=Charm._REPLICA_SECRET_LABEL).latest_content
        credential = next(iter(content.values()))
        execute = mocker.patch("ops.Container.exec", autospec=True)
        execute.return_value.wait_output.return_value = (
            f"progress: {credential}\n",
            f"parse error: {credential}\n",
        )
        if exit_code:
            execute.return_value.wait_output.side_effect = ops.pebble.ExecError(
                ["example-command"],
                exit_code,
                f"progress: {credential}\n",
                f"parse error: {credential}\n",
            )
        event = ctx.on.action("run-maintenance-script", params={"script": "showJobs"})

        if exit_code:
            with pytest.raises(testing.ActionFailed) as raised:
                ctx.run(event, base_state)
            assert credential not in str(raised.value)
            assert "progress: [REDACTED]" in str(raised.value)
            assert "parse error: [REDACTED]" in str(raised.value)
        else:
            ctx.run(event, base_state)
            assert ctx.action_results == {"output": "progress: [REDACTED]\n"}
        assert credential not in _output(log_sink)

    def test_ssh_keys_seeded_before_standalone_action(self, ctx, base_state, log_sink):
        key = "-----BEGIN PRIVATE KEY-----\nssh-key-payload\n-----END PRIVATE KEY-----"
        secret = testing.Secret({"mediawiki": key, "git-sync": key})
        state = dataclasses.replace(
            base_state, secrets=[*base_state.secrets, secret], config={"ssh-key": secret.id}
        )

        with ctx(ctx.on.action("rotate-mediawiki-secrets"), state) as manager:
            logging.getLogger("ssh").error("ssh-key-payload")
            manager.run()

        assert "ssh-key-payload" not in _output(log_sink)
        assert "ERROR ssh: [REDACTED]" in _output(log_sink)

    def test_rotation_retains_old_and_new_values(self, ctx, base_state, log_sink):
        old_content = base_state.get_secret(label=Charm._REPLICA_SECRET_LABEL).latest_content
        with ctx(ctx.on.action("rotate-mediawiki-secrets"), base_state) as manager:
            manager.charm._peers.rotate_secrets()
            new_content = manager.charm.model.get_secret(
                label=Charm._REPLICA_SECRET_LABEL
            ).get_content()
            logging.getLogger("rotation").error("%r %r", old_content, new_content)
            manager.run()

        output = _output(log_sink)
        for value in (*old_content.values(), *new_content.values()):
            assert value not in output
        assert "[REDACTED]" in output

    def test_migration_registers_new_values(self, ctx, active_state, log_sink):
        content = dict(active_state.get_secret(label=Charm._REPLICA_SECRET_LABEL).latest_content)
        content.pop("authentication-token-version")
        secret = testing.Secret(content, label=Charm._REPLICA_SECRET_LABEL, owner="app")
        state = dataclasses.replace(active_state, secrets=[secret])

        with ctx(ctx.on.update_status(), state) as manager:
            values = manager.charm._peers.reconciliation_state().secrets.to_juju_secret()
            logging.getLogger("migration").error("%r", values)
            manager.run()

        for value in values.values():
            assert value not in _output(log_sink)

    def test_secret_changed_seeds_latest_without_advancing_tracking(
        self, ctx, base_state, log_sink
    ):
        secret = testing.Secret(
            {"password": "tracked-credential"},  # nosec: B105
            latest_content={"password": "latest-credential"},  # nosec: B105
            remote_grants={0: {"mediawiki"}},
        )
        relation = testing.Relation(
            endpoint="database", id=0, remote_app_data={"secret-user": secret.id}
        )
        state = dataclasses.replace(
            base_state, secrets=[*base_state.secrets, secret], relations=[relation]
        )
        with ctx(ctx.on.secret_changed(secret), state) as manager:
            logging.getLogger("changed").error("tracked-credential latest-credential")
            assert manager.charm.model.get_secret(id=secret.id).get_content() == {
                "password": "tracked-credential",  # nosec: B105
            }

        assert "tracked-credential" not in _output(log_sink)
        assert "latest-credential" not in _output(log_sink)

    def test_s3_validation_error_is_masked(self, ctx, base_state, log_sink):
        relation = testing.Relation(
            endpoint="s3-parameters",
            remote_app_data={
                "access-key": "s3-access-credential",
                "secret-key": "s3-private-credential",
            },
        )
        state = dataclasses.replace(base_state, relations=[relation])
        with ctx(ctx.on.update_status(), state) as manager:
            with pytest.raises(
                MediaWikiBlockedStatusException, match="Error fetching s3-parameters"
            ):
                manager.charm._s3.get_relation_data()
            logging.getLogger("s3").error("s3-access-credential s3-private-credential")

        assert "s3-access-credential" not in _output(log_sink)
        assert "s3-private-credential" not in _output(log_sink)
        assert "Failed to parse s3 relation data" in _output(log_sink)

    def test_tls_key_registered_before_certificate_is_available(
        self, ctx, base_state, log_sink, mocker
    ):
        key = PrivateKey.generate()
        mocker.patch(
            "relations.tls.TLSCertificatesRequiresV4.get_private_key",
            autospec=True,
            return_value=key,
        )
        with ctx(ctx.on.update_status(), base_state) as manager:
            assert manager.charm._tls.get_material() is None
            logging.getLogger("tls").exception("Private key: %s", key.raw)

        output = _output(log_sink)
        assert key.raw not in output
        assert "Private key: [REDACTED]" in output

    @pytest.mark.parametrize("combine_stderr", [True, False])
    @pytest.mark.parametrize("sensitive", [True, False])
    @pytest.mark.parametrize("exit_code", [0, 1])
    def test_container_command_output_is_masked_before_returning(
        self, log_sink, mocker, combine_stderr, sensitive, exit_code
    ):
        container = mocker.create_autospec(ops.Container, instance=True)
        container.exec.return_value.wait_output.return_value = (
            "stdout-credential",
            "stderr-credential",
        )
        if exit_code:
            container.exec.return_value.wait_output.side_effect = ops.pebble.ExecError(
                ["example-command"], exit_code, "stdout-credential", "stderr-credential"
            )
        log_redaction.register_secrets("stdout-credential", "stderr-credential")
        service = ContainerService(container)

        result = service._run_cli(
            ["example-command"], sensitive=sensitive, combine_stderr=combine_stderr
        )

        assert result == CommandExecResult(exit_code, "[REDACTED]", "[REDACTED]")
        if exit_code:
            with pytest.raises(MediaWikiInstallError, match=r"\[REDACTED\]") as raised:
                result.raise_for_status(
                    "Running command", MediaWikiInstallError, include_stderr=True
                )
            assert "stderr-credential" not in str(raised.value)
        output = _output(log_sink)
        assert "stdout-credential" not in output
        assert "stderr-credential" not in output
        if sensitive:
            assert "example-command" not in output
            assert "REDACTED SENSITIVE COMMAND" in output
        else:
            assert "example-command" in output

    @pytest.mark.parametrize("stderr", [None, b"", b"\xffstderr-credential"])
    def test_failed_command_byte_output_preserves_type_and_unrelated_bytes(
        self, log_sink, mocker, stderr
    ):
        container = mocker.create_autospec(ops.Container, instance=True)
        container.exec.return_value.wait_output.side_effect = ops.pebble.ExecError(
            ["example-command"], 2, b"\xffstdout-credential", stderr
        )
        log_redaction.register_secrets("stdout-credential", "stderr-credential")

        result = ContainerService(container)._run_cli(["example-command"])

        assert result.stdout == b"\xff[REDACTED]"
        assert result.stderr == (b"\xff[REDACTED]" if stderr else stderr)
        assert result.return_code == 2

    def test_valkey_password_is_masked_before_failed_validation(self, ctx, base_state, log_sink):
        relation = testing.Relation(
            endpoint="valkey",
            remote_app_data={
                "version": '"v1"',
                "requests": '[{"password":"valkey-validation-credential","endpoints":123}]',
            },
        )
        state = dataclasses.replace(base_state, relations=[relation])
        with ctx(ctx.on.update_status(), state) as manager:
            assert manager.charm._valkey.get_connection_info() is None
            logging.getLogger("valkey").error("valkey-validation-credential")

        output = _output(log_sink)
        assert "valkey-validation-credential" not in output
        assert "Could not read Valkey response" in output
        assert "ValidationError" in output


class TestUnavailableCredentials:
    def test_missing_referenced_secret_reports_safe_warning(self, log_sink, mocker):
        model = mocker.create_autospec(ops.Model, instance=True)
        model.get_secret.side_effect = ops.SecretNotFoundError("unsafe-payload")

        log_redaction.register_secret(model, secret_id="secret:id", fields=("password",))  # nosec: B106
        assert "could not read a referenced secret" in _output(log_sink)
        assert "unsafe-payload" not in _output(log_sink)
        log_sink.reset_mock()
        log_redaction.register_secret(model, label="optional", fields=("password",))
        log_sink.juju_log.assert_not_called()

    def test_latest_revision_failure_still_masks_tracked_value(self, log_sink, mocker):
        model = mocker.create_autospec(ops.Model, instance=True)
        model.get_secret.return_value.get_content.return_value = {"password": "tracked-value"}  # nosec: B105
        model.get_secret.return_value.peek_content.side_effect = ops.ModelError("unsafe-payload")

        log_redaction.register_secret(model, secret_id="secret:id", fields=("password",))  # nosec: B106
        logging.getLogger("test").error("tracked-value")

        assert "tracked-value" not in _output(log_sink)
        assert "unsafe-payload" not in _output(log_sink)
        assert "could not read a secret (ModelError)" in _output(log_sink)

    @pytest.mark.parametrize("requests", ["invalid-json", '"not-a-list"', "[null, 1]"])
    def test_malformed_valkey_data_has_safe_diagnostics(self, ctx, base_state, log_sink, requests):
        relation = testing.Relation(endpoint="valkey", remote_app_data={"requests": requests})
        state = dataclasses.replace(base_state, relations=[relation])
        with ctx(ctx.on.update_status(), state) as manager:
            assert manager.charm._valkey.get_connection_info() is None

        assert "Could not read Valkey response" in _output(log_sink)
