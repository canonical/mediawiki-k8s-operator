# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Mask explicitly registered credentials in log output and command results."""

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar, Protocol

import ops

from utils import escape_php_string

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RelationRedaction:
    """Describe which relation application data fields carry credentials.

    Attributes:
        fields: Fields holding credentials, in relation data or in referenced secrets.
        references: Fields holding Juju secret IDs whose content includes ``fields``.
        nested_list_key: Field holding a JSON list of objects using the same field names.
    """

    fields: tuple[str, ...]
    references: tuple[str, ...] = ()
    nested_list_key: str | None = None


class RelationRedactionSource(Protocol):
    """A relation handler declaring how to redact its relation data."""

    REDACTION: ClassVar[RelationRedaction]


class SecretRedactor:
    """Hold known secret representations for one charm invocation, only in memory."""

    def __init__(self) -> None:
        self._values: set[str] = set()
        self._pattern: re.Pattern[str] | None = None

    def register(self, *values: str | None) -> None:
        """Register literal values and their common log/config representations."""
        previous_size = len(self._values)
        for value in values:
            # Ignore empty/whitespace-only values to avoid masking log formatting.
            if value is None or not value.strip():
                continue

            variants = {value}
            if "PRIVATE KEY-----" in value:
                # Errors may expose a single payload line; delimiters are not secret.
                variants.update(
                    line.strip()
                    for line in value.splitlines()
                    if line.strip() and not line.startswith("-----")
                )

            for variant in variants:
                self._values.update(
                    {
                        variant,
                        # Match escaped Python strings/bytes without their outer quotes.
                        repr(variant)[1:-1],
                        repr(variant.encode())[2:-1],
                        # JSON uses different quote and Unicode escapes.
                        json.dumps(variant)[1:-1],
                        # Generated PHP configuration uses single-quoted strings.
                        escape_php_string(variant),
                    }
                )
        if len(self._values) != previous_size:
            # Rebuild the cached expression when newly registered values are used.
            self._pattern = None

    def redact(self, text: str) -> str:
        """Replace known values in one pass, preferring longer overlapping matches."""
        if not self._values:
            return text
        if self._pattern is None:
            values = sorted(self._values, key=lambda value: (-len(value), value))
            self._pattern = re.compile("|".join(re.escape(value) for value in values))
        return self._pattern.sub(lambda _: "[REDACTED]", text)


class _RedactingFormatter(logging.Formatter):
    """Apply masking after the original formatter has included exception/stack text."""

    def __init__(self, original: logging.Formatter, redactor: SecretRedactor) -> None:
        super().__init__()
        self.original = original
        self.redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        """Render with the original formatter and mask the complete output."""
        return self.redactor.redact(self.original.format(record))


_redactor = SecretRedactor()


def install() -> None:
    """Start a fresh invocation and wrap the existing root handler formatters."""
    global _redactor
    _redactor = SecretRedactor()
    for handler in logging.getLogger().handlers:
        formatter = handler.formatter or logging.Formatter()
        if isinstance(formatter, _RedactingFormatter):
            formatter = formatter.original
        handler.setFormatter(_RedactingFormatter(formatter, _redactor))


def register_secrets(*values: str | None) -> None:
    """Register credentials before they are used by code that might log them."""
    _redactor.register(*values)


def redact(text: str | bytes) -> str | bytes:
    """Mask known secrets, preserving text type and any non-UTF-8 bytes."""
    if isinstance(text, bytes):
        return _redactor.redact(text.decode("utf-8", errors="surrogateescape")).encode(
            "utf-8", errors="surrogateescape"
        )
    return _redactor.redact(text)


def register_secret(
    model: ops.Model,
    *,
    fields: tuple[str, ...],
    secret_id: str | None = None,
    label: str | None = None,
) -> None:
    """Register secrets from a Juju secret for log redaction."""
    try:
        secret = model.get_secret(id=secret_id, label=label)
        for read_content in (secret.get_content, secret.peek_content):
            content = read_content()
            register_secrets(*(content.get(field) for field in fields))
    except ops.SecretNotFoundError:
        if secret_id:
            logger.warning("Log masking could not read a referenced secret; it may be removed.")
    except ops.ModelError as error:
        # Retrieval errors can include credentials; never render the error payload here.
        logger.warning("Log masking could not read a secret (%s).", type(error).__name__)


def register_relation_secrets(
    model: ops.Model,
    relation: ops.Relation,
    source: type[RelationRedactionSource],
) -> None:
    """Register secrets from a Juju relation for log redaction."""
    if relation.app is None:
        return
    data = relation.data[relation.app]
    redaction = source.REDACTION
    fields, references = redaction.fields, redaction.references
    _register_fields(model, data, fields, references)
    if redaction.nested_list_key is None:
        return
    try:
        nested = json.loads(data.get(redaction.nested_list_key, "[]"))
    except json.JSONDecodeError:
        logger.warning("Log masking could not parse nested relation data.")
        return
    if isinstance(nested, list):
        for item in nested:
            if isinstance(item, dict):
                _register_fields(model, item, fields, references)


def _register_fields(
    model: ops.Model,
    data: Mapping[str, object],
    fields: tuple[str, ...],
    references: tuple[str, ...],
) -> None:
    """Register only named fields, including fields stored in referenced secrets."""
    for field in fields:
        value = data.get(field)
        if isinstance(value, str):
            register_secrets(value)
    for reference in references:
        secret_id = data.get(reference)
        if isinstance(secret_id, str) and secret_id:
            register_secret(model, secret_id=secret_id, fields=fields)
