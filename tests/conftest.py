# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Fixtures for charm tests."""


def pytest_addoption(parser):
    """Parse additional pytest options.

    Args:
        parser: Pytest parser.
    """
    parser.addoption("--charm-file", action="store")
    parser.addoption(
        "--keep-models",
        action="store_true",
        default=False,
        help="keep temporarily-created models",
    )
    parser.addoption(
        "--use-existing",
        action="store_true",
        default=False,
        help="use existing models and not created models",
    )
    parser.addoption(
        "--model",
        action="store",
        help="temporarily-created model name",
    )
    parser.addoption(
        "--mediawiki-image",
        action="store",
        help="MediaWiki OCI image built for the MediaWiki charm",
    )
    parser.addoption(
        "--num-units",
        action="store",
        type=int,
        default=3,
        help="Number of MediaWiki units to deploy (default: 3)",
    )


def pytest_configure(config):
    """Adds config options."""
    config.addinivalue_line("markers", "abort_on_fail")
