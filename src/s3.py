# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Provides the S3 class to handle s3 relations and state."""

import logging

from charms.data_platform_libs.v0.s3 import S3Requirer
from ops import CharmBase, Object
from pydantic import ValidationError

from exceptions import MediaWikiBlockedStatusException
from types_ import S3ConnectionInfo

logger = logging.getLogger(__name__)


class S3(Object):
    """The S3 relation handler."""

    def __init__(self, charm: CharmBase, relation_name: str):
        """Initialize the handler and register event handlers.

        Args:
            charm: The charm instance.
            relation_name: The name of the s3-parameters relation.
        """
        super().__init__(charm, "s3-observer")

        self.s3 = S3Requirer(charm, relation_name)

    def get_relation_data(self) -> S3ConnectionInfo:
        """Get the s3 relation data.

        If the relation data is incomplete or malformed, an exception is raised.

        Returns:
            S3ConnectionInfo: The s3 relation data.

        Raises:
            MediaWikiBlockedStatusException: If the relation missing or the data is malformed.
        """
        if not self.has_relation():
            raise MediaWikiBlockedStatusException(f"Waiting for relation {self.s3.relation_name}.")

        try:
            # We have to type-ignore here because the s3 lib's type annotation is wrong
            return S3ConnectionInfo(**self.s3.get_s3_connection_info())  # type: ignore
        except ValidationError as e:
            logger.warning(f"Failed to parse s3 relation data: {e}")
            raise MediaWikiBlockedStatusException(
                f"Error fetching {self.s3.relation_name} relation data."
            )

    def has_relation(self) -> bool:
        """Check if the relation exists."""
        return (
            self.model.get_relation(self.s3.relation_name) is not None
            and len(self.s3.relations) > 0
        )
