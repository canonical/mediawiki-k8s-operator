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
            "url_origin": "//wiki.example.com",
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
        "url_origin, expected",
        [
            ("", ""),
            ("//wiki.example.com", "//wiki.example.com"),
            ("//wiki.example.com:8080", "//wiki.example.com:8080"),
            ("//192.168.1.10", "//192.168.1.10"),
            ("//[2001:db8::1]:8443", "//[2001:db8::1]:8443"),
            ("http://wiki.example.com", "http://wiki.example.com"),
            ("https://wiki.example.com", "https://wiki.example.com"),
            ("https://wiki.example.com:443", "https://wiki.example.com:443"),
            ("http://192.168.1.10", "http://192.168.1.10"),
            (" https://wiki.example.com ", "https://wiki.example.com"),
        ],
    )
    def test_url_origin_accepts_valid_values(self, url_origin: str, expected: str) -> None:
        config = self.make_config(url_origin=url_origin)

        assert config.url_origin == expected

    @pytest.mark.parametrize(
        "url_origin, error_match",
        [
            ("wiki.example.com", "url-origin must be"),
            ("wiki.example.com:8080", "url-origin must be"),
            ("ftp://wiki.example.com", "url-origin must be"),
            ("//wiki.example.com/path", "unexpected components"),
            ("http://wiki.example.com/path", "unexpected components"),
            ("//wiki!.example.com", "not valid"),
            ("//wiki.example.com:65536", "Could not parse"),
            ("https://wiki.example.com/", "unexpected components"),
            ("https://wiki.example.com?x=1", "unexpected components"),
            ("https://wiki.example.com#frag", "unexpected components"),
            ("https://wiki.example.com#frag", "unexpected components"),
            ("//user@wiki.example.com", "unexpected components"),
            ("https://user:pass@wiki.example.com", "unexpected components"),
        ],
    )
    def test_url_origin_rejects_invalid_values(self, url_origin: str, error_match: str) -> None:
        with pytest.raises(ValidationError, match=error_match):
            self.make_config(url_origin=url_origin)
