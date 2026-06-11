# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Generic base class for managing an ops container workload."""

from __future__ import annotations

import logging
from typing import List, Union, cast

import ops

from exceptions import ContainerError
from types_ import CommandExecResult

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 60


class ContainerService:
    """Base class for managing a workload running in an ops container.

    Wraps an :class:`ops.Container` and provides a generic helper for executing
    commands inside it. Subclass this for each container-backed workload so that
    the command-execution logic can be shared across multiple containers.
    """

    def __init__(self, container: ops.Container) -> None:
        """Initialize the container service.

        Args:
            container: The ops container this service manages.
        """
        self._container = container

    def _run_cli(
        self,
        cmd: List[str],
        *,
        environment: dict[str, str] | None = None,
        user: Union[str, None] = None,
        group: Union[str, None] = None,
        working_dir: Union[str, None] = None,
        combine_stderr: bool = False,
        timeout: int = _DEFAULT_TIMEOUT,
        sensitive: bool = False,
    ) -> CommandExecResult:
        """Execute a command in the managed container.

        Args:
            cmd (List[str]): The command to be executed.
            environment (dict[str, str], optional): Environment variables to set for the command. Defaults to None.
            user (str): Username to run this command as, use root when not provided.
            group (str): Name of the group to run this command as, use root when not provided.
            working_dir (str):  Working dir to run this command in, use home dir if not provided.
            combine_stderr (bool): Redirect stderr to stdout, when enabled, stderr in the result
                will always be empty.
            timeout (int): Set a timeout for the running program in seconds.
                ``ContainerError`` will be raised if timeout exceeded.
            sensitive (bool): Whether the command contains sensitive information, such as passwords. If True, the command will be redacted in logs.

        Returns:
            A named tuple with three fields: return code, stdout and stderr. Stdout and stderr are
            both string.

        Raises:
            ContainerError: If the command execution times out.
        """
        cmd_preview = cmd
        if sensitive:
            cmd_preview = ["REDACTED SENSITIVE COMMAND"]

        process = self._container.exec(
            cmd,
            environment=environment,
            user=user,
            group=group,
            working_dir=working_dir,
            combine_stderr=combine_stderr,
            timeout=timeout,
        )
        try:
            stdout, stderr = process.wait_output()
            result = CommandExecResult(return_code=0, stdout=stdout, stderr=stderr)
        except ops.pebble.ExecError as error:
            result = CommandExecResult(
                error.exit_code,
                cast(Union[str, bytes], error.stdout),
                cast(Union[str, bytes, None], error.stderr),
            )
        except TimeoutError:
            logger.error("Command timed out after %s seconds: %s", timeout, cmd_preview)

            raise ContainerError("Container command execution timed out; see logs for details.")

        return_code = result.return_code
        if combine_stderr:
            logger.debug(
                "Run command: %s return code %s\noutput: %s",
                cmd_preview,
                return_code,
                result.stdout,
            )
        else:
            logger.debug(
                "Run command: %s, return code %s\nstdout: %s\nstderr:%s",
                cmd_preview,
                return_code,
                result.stdout,
                result.stderr,
            )
        return result
