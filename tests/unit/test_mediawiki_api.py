# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import string

import requests
from pytest_mock import MockerFixture

import mediawiki_api
from mediawiki_api import SiteInfo


class TestApiQuery:
    def test_success(self, mocker: MockerFixture) -> None:
        """Test that a successful API query returns parsed JSON."""
        mock_response = mocker.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"query": {"general": {"generator": "Mocked Version"}}}
        mock_get = mocker.patch("mediawiki_api.requests.get", return_value=mock_response)

        result = mediawiki_api._api_query(action="query", meta="siteinfo")
        assert result == {"query": {"general": {"generator": "Mocked Version"}}}
        mock_get.assert_called_once_with(
            "http://localhost/w/api.php",
            params={"action": "query", "meta": "siteinfo", "format": "json", "formatversion": "2"},
            timeout=mediawiki_api._REQUEST_TIMEOUT,
        )

    def test_injects_format_json(self, mocker: MockerFixture) -> None:
        """Test that format=json is always injected."""
        mock_response = mocker.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {}
        mock_get = mocker.patch("mediawiki_api.requests.get", return_value=mock_response)

        mediawiki_api._api_query(action="query")
        call_params = mock_get.call_args[1]["params"]
        assert call_params["format"] == "json"
        assert call_params["formatversion"] == "2"

    def test_formatversion_override(self, mocker: MockerFixture) -> None:
        """Test that formatversion can be overridden."""
        mock_response = mocker.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {}
        mock_get = mocker.patch("mediawiki_api.requests.get", return_value=mock_response)

        mediawiki_api._api_query(action="query", formatversion="1")
        call_params = mock_get.call_args[1]["params"]
        assert call_params["formatversion"] == "1"

    def test_request_failure(self, mocker: MockerFixture) -> None:
        """Test that an empty dict is returned when the request fails."""
        mock_response = mocker.Mock()
        mock_response.status_code = 500
        mocker.patch("mediawiki_api.requests.get", return_value=mock_response)

        assert mediawiki_api._api_query(action="query") == {}

    def test_request_exception(self, mocker: MockerFixture) -> None:
        """Test that an empty dict is returned on a network error."""
        mocker.patch(
            "mediawiki_api.requests.get",
            side_effect=requests.exceptions.ConnectionError("Mocked connection error"),
        )
        assert mediawiki_api._api_query(action="query") == {}

    def test_not_json_response(self, mocker: MockerFixture) -> None:
        """Test that an empty dict is returned on a non-JSON response."""
        mock_response = mocker.Mock()
        mock_response.status_code = 200
        mock_response.json.side_effect = requests.exceptions.JSONDecodeError(
            "Mocked JSON decode error", "", 0
        )
        mocker.patch("mediawiki_api.requests.get", return_value=mock_response)

        assert mediawiki_api._api_query(action="query") == {}


class TestSiteInfoFetch:
    def test_fetch(self, mocker: MockerFixture) -> None:
        """Test that fetch calls _api_query and wraps the query dict."""
        mock_api = mocker.patch(
            "mediawiki_api._api_query",
            return_value={"query": {"general": {"generator": "MediaWiki 1.45.1"}}},
        )
        info = SiteInfo.fetch()
        assert info.version == "mediawiki-1.45.1"
        mock_api.assert_called_once_with(
            action="query", meta="siteinfo", siprop="general|namespaces"
        )

    def test_fetch_empty_response(self, mocker: MockerFixture) -> None:
        """Test that an empty API response produces a usable SiteInfo."""
        mocker.patch("mediawiki_api._api_query", return_value={})
        info = SiteInfo.fetch()
        assert info.version == ""
        assert info.article_url is None
        assert info.special_namespace_name is None


class TestSiteInfoVersion:
    def test_version(self) -> None:
        """Test that the version is extracted and formatted correctly."""
        info = SiteInfo({"general": {"generator": "MediaWiki 1.45.1"}})
        assert info.version == "mediawiki-1.45.1"

    def test_no_generator(self) -> None:
        """Test that an empty string is returned when generator is absent."""
        info = SiteInfo({})
        assert info.version == ""


class TestSiteInfoArticleUrl:
    def test_returns_template(self) -> None:
        """Test that a string.Template combining server and article path is returned."""
        info = SiteInfo({"general": {"server": "https://example.com", "articlepath": "/wiki/$1"}})
        tmpl = info.article_url
        assert isinstance(tmpl, string.Template)
        assert tmpl.template == "https://example.com/wiki/${article}"

    def test_article_substitution(self) -> None:
        """Test that the template substitutes article names correctly."""
        info = SiteInfo({"general": {"server": "https://example.com", "articlepath": "/wiki/$1"}})
        tmpl = info.article_url
        assert tmpl is not None
        assert tmpl.substitute(article="Main_Page") == "https://example.com/wiki/Main_Page"

    def test_no_general(self) -> None:
        """Test that None is returned when general info is unavailable."""
        info = SiteInfo({})
        assert info.article_url is None

    def test_missing_server(self) -> None:
        """Test that None is returned when articlepath is relative and server is absent."""
        info = SiteInfo({"general": {"articlepath": "/wiki/$1"}})
        assert info.article_url is None

    def test_absolute_articlepath(self) -> None:
        """Test that an absolute articlepath is used as-is without prepending server."""
        info = SiteInfo({"general": {"articlepath": "https://other.example.com/wiki/$1"}})
        tmpl = info.article_url
        assert tmpl is not None
        assert tmpl.substitute(article="Main_Page") == "https://other.example.com/wiki/Main_Page"

    def test_protocol_relative_articlepath(self) -> None:
        """Test that a protocol-relative articlepath is used as-is without prepending server."""
        info = SiteInfo({"general": {"articlepath": "//other.example.com/wiki/$1"}})
        tmpl = info.article_url
        assert tmpl is not None
        assert tmpl.substitute(article="Main_Page") == "//other.example.com/wiki/Main_Page"

    def test_missing_articlepath(self) -> None:
        """Test that None is returned when the articlepath field is absent."""
        info = SiteInfo({"general": {"server": "https://example.com"}})
        assert info.article_url is None


class TestSiteInfoSpecialNamespaceName:
    def test_localized_name(self) -> None:
        """Test that the localized Special namespace name is returned."""
        info = SiteInfo(
            {
                "namespaces": {
                    "-2": {
                        "id": -2,
                        "case": "first-letter",
                        "canonical": "Media",
                        "name": "Média",
                    },
                    "-1": {
                        "id": -1,
                        "case": "first-letter",
                        "canonical": "Special",
                        "name": "Spécial",
                    },
                }
            }
        )
        assert info.special_namespace_name == "Spécial"

    def test_english_name(self) -> None:
        """Test that the English Special namespace name is returned."""
        info = SiteInfo(
            {
                "namespaces": {
                    "-1": {
                        "id": -1,
                        "case": "first-letter",
                        "canonical": "Special",
                        "name": "Special",
                    },
                }
            }
        )
        assert info.special_namespace_name == "Special"

    def test_no_namespaces(self) -> None:
        """Test that None is returned when namespaces are unavailable."""
        info = SiteInfo({})
        assert info.special_namespace_name is None

    def test_no_special_namespace(self) -> None:
        """Test that None is returned when the Special namespace is absent."""
        info = SiteInfo({"namespaces": {"0": {"id": 0, "case": "first-letter", "name": ""}}})
        assert info.special_namespace_name is None
