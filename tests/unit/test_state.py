# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.


from typing import Any

import pytest
from pydantic import ValidationError

from state import CharmConfig


class TestCharmConfig:
    """Tests for CharmConfig validators."""

    @staticmethod
    def make_config(**overrides: Any) -> CharmConfig:
        """Build a CharmConfig with test defaults and optional overrides."""
        base_config: dict[str, Any] = {
            "composer": "{}",
            "static_assets_git_repo": "",
            "static_assets_git_ref": "",
            "hostname": "wiki.example.com",
            "local_settings": "",
            "robots_txt": "",
        }

        return CharmConfig(**(base_config | overrides))

    def test_composer_accepts_json_object(self) -> None:
        config = self.make_config(composer='  {"require": {"a/b": "^1.0"}}  ')

        assert config.composer == {"require": {"a/b": "^1.0"}}

    @pytest.mark.parametrize("composer", ["[]", '"str"', "1", "true", "null"])
    def test_composer_rejects_non_object_json(self, composer: str) -> None:
        with pytest.raises(ValidationError, match="Composer configuration must be a JSON object"):
            self.make_config(composer=composer)

    def test_composer_rejects_invalid_json(self) -> None:
        with pytest.raises(ValidationError, match="Composer configuration must be a JSON object"):
            self.make_config(composer="{not-json}")

    @pytest.mark.parametrize(
        "hostname",
        [
            "wiki.example.com",
            "wiki.example.com:8080",
            "192.168.1.10",
            "[2001:db8::1]:8443",
            "",
        ],
    )
    def test_hostname_accepts_valid_values(self, hostname: str) -> None:
        config = self.make_config(hostname=hostname)

        assert config.hostname == hostname

    @pytest.mark.parametrize(
        "hostname, error_match",
        [
            ("http://wiki.example.com", "schema or path component"),
            ("http://192.168.1.10", "schema or path component"),
            ("wiki.example.com/path", "schema or path component"),
            ("wiki!.example.com", "Hostname is not a valid"),
            ("wiki.example.com:notaport", "Failed to validate hostname"),
            ("wiki.example.com:65536", "Failed to validate hostname"),
        ],
    )
    def test_hostname_rejects_invalid_values(self, hostname: str, error_match: str) -> None:
        with pytest.raises(ValidationError, match=error_match):
            self.make_config(hostname=hostname)
