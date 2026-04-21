# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Provides the OAuth class to handle OAuth relations and state."""

import logging
from urllib.parse import urlparse

from charms.hydra.v0.oauth import ClientConfig, OauthProviderConfig, OAuthRequirer
from ops import Object

from exceptions import MediaWikiBlockedStatusException
from mediawiki_api import SiteInfo
from state import StatefulCharmBase

logger = logging.getLogger(__name__)


class OAuth(Object):
    """The OAuth relation handler."""

    _base_scope = frozenset({"openid", "profile", "email"})
    _grant_types = frozenset({"authorization_code", "client_credentials", "refresh_token"})

    _redirect_page_title = "PluggableAuthLogin"

    def __init__(self, charm: StatefulCharmBase, relation_name: str):
        """Initialize the handler and register event handlers.

        Args:
            charm: The charm instance.
            relation_name: The name of the oauth relation.
        """
        super().__init__(charm, "oauth-observer")

        self._charm = charm
        self.oauth = OAuthRequirer(self._charm, relation_name=relation_name)
        self.relation_name = relation_name

    def scopes(self) -> frozenset[str]:
        """Get the set of scopes we want to request from the provider.

        Returns:
            A set of OAuth scopes we want to request from the provider, or an empty set if no scopes are specified.
        """
        config = self._charm.load_charm_config()
        extra_scopes = config.oauth_extra_scopes.split()

        return self._base_scope | frozenset(extra_scopes)

    def update_client_config(
        self,
    ) -> None:
        """Update the client config from the relation. Does nothing if the unit is not the leader.

        Note, if a protocol-relative URL is used, we fall back to HTTPS for the redirect URI.

        Does nothing if the relation is not established.

        Raises:
            MediaWikiBlockedStatusException: If the client config update fails.
        """
        if not self._charm.unit.is_leader():
            return

        if self.model.get_relation(self.relation_name) is None:
            logger.debug("OAuth relation is not established, skipping client config update")
            return

        site_info = SiteInfo.fetch()
        article_url_template = site_info.article_url
        namespace = site_info.special_namespace_name

        if article_url_template is None:
            logger.warning("Article URL template is unavailable, cannot construct redirect URI")
            raise MediaWikiBlockedStatusException("Failed to query article URL from MediaWiki API")

        if namespace is None:
            logger.warning(
                "Special namespace name is unavailable, falling back to canonical name 'Special' for redirect URI"
            )
            namespace = "Special"

        redirect_uri = article_url_template.substitute(
            article=f"{namespace}:{self._redirect_page_title}"
        )

        client_config = ClientConfig(
            urlparse(redirect_uri, scheme="https").geturl(),
            " ".join(sorted(self.scopes())),
            list(self._grant_types),
        )
        try:
            self.oauth.update_client_config(client_config)
        except Exception as e:
            logger.error("Failed to update OAuth client config: %s", e)
            raise MediaWikiBlockedStatusException("Failed to update OAuth client config") from e

    def get_provider_info(self) -> OauthProviderConfig | None:
        """Get the provider info from the relation."""
        return self.oauth.get_provider_info()
