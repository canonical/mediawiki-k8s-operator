# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""User-defined exceptions used by MediaWiki charm."""

import ops.model


# This exception is used to signal the early termination of a reconciliation process.
# The early termination can be caused by many things like relation is not ready or config is not
# updated, and may turn the charm into waiting or block state. They are inevitable in the early
# stage of the charm's lifecycle, thus this is not an error (N818), same for all the subclasses.
class MediaWikiStatusException(Exception):  # noqa: N818
    """Exception to signal an early termination of the reconciliation.

    ``status`` represents the status change comes with the early termination.
    Do not instantiate this class directly, use subclass instead.
    """

    _status_class = ops.model.StatusBase

    def __init__(self, message: str):
        """Initialize the instance.

        Args:
            message: A message explaining the reason for given exception.

        Raises:
            TypeError: if same base class is used to instantiate base class.
        """
        # Using type is necessary to check types between subclasses and superclass.
        # pylint: disable=unidiomatic-typecheck
        if type(self) is MediaWikiStatusException:
            raise TypeError("Instantiating a base class: MediaWikiStatusException")
        super().__init__(message)
        self.status = self._status_class(message)


class MediaWikiBlockedStatusException(MediaWikiStatusException):
    """Same as :exc:`exceptions.MediaWikiStatusException`."""

    _status_class = ops.model.BlockedStatus


class MediaWikiWaitingStatusException(MediaWikiStatusException):
    """Same as :exc:`exceptions.MediaWikiStatusException`."""

    _status_class = ops.model.WaitingStatus


class MediaWikiInstallError(Exception):
    """Exception for unrecoverable errors during MediaWiki installation."""


class ContainerError(Exception):
    """Exception for unrecoverable errors while executing a container command."""


class CharmConfigInvalidError(Exception):
    """Exception raised when a charm configuration is found to be invalid.

    Attributes:
        msg: Explanation of the error.
    """

    def __init__(self, msg: str):
        """Initialize a new instance of the CharmConfigInvalidError exception.

        Args:
            msg: Explanation of the error.
        """
        self.msg = msg
        super().__init__(msg)
