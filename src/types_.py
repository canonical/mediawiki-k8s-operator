# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Module for internal types for the MediaWiki charm."""

from string import Template
from typing import List, NamedTuple, Optional, Union

from pydantic import BaseModel, Field


class CommandExecResult(NamedTuple):
    """Result of executed command from the MediaWiki container.

    Attrs:
        return_code: exit code from executed command.
        stdout: standard output from the executed command.
        stderr: standard error output from the executed command.
    """

    return_code: int
    stdout: Union[str, bytes]
    stderr: Union[str, bytes, None]


class DatabaseEndpoint(NamedTuple):
    """Endpoint details for database connection.

    Attrs:
        host: The hostname under which the database is being served.
        port: The port which the database is listening on.
    """

    host: str
    port: int

    @classmethod
    def from_string(cls, endpoint: str) -> "DatabaseEndpoint":
        """Create a DatabaseEndpoint instance from a string.

        Args:
            endpoint: The endpoint string in the format "host:port".

        Returns:
            DatabaseEndpoint: The created DatabaseEndpoint instance.

        Raises:
            ValueError: If the endpoint string is malformed.
        """
        parts = endpoint.strip().split(":")
        if len(parts) == 1:
            return cls(host=parts[0], port=0)
        if len(parts) == 2:
            return cls(host=parts[0], port=int(parts[1]))
        raise ValueError(f"Malformed endpoint: {endpoint}")

    def to_string(self) -> str:
        """Return the host string for database connection, formatted with the port if available."""
        return f"{self.host}:{self.port}" if self.port else self.host


class DatabaseConfig(NamedTuple):
    """Configuration values required to connect to database.

    Attrs:
        endpoints: The read/write endpoints under which the database is being served.
        database: The name of the database to connect to.
        username: The username to use to authenticate to the database.
        password: The password to use to authenticate to the database.
    """

    endpoints: tuple[DatabaseEndpoint, ...]
    database: str
    username: str
    password: str

    def ready(self) -> bool:
        """Check if the database relation data is complete.

        Returns:
            bool: True if all required fields are populated, False otherwise.
        """
        return bool(self.database and self.username and self.password and self.endpoints)


class PhpTemplate(Template):
    """A PHP safe(r) string template using '%' as the delimiter."""

    delimiter = "%"


class S3ConnectionInfo(BaseModel):
    """Model for the s3 relation databag, as returned by the s3 charm lib."""

    model_config = {"populate_by_name": True}

    endpoint: str
    bucket: str
    access_key: str = Field(alias="access-key")
    secret_key: str = Field(alias="secret-key")

    region: Optional[str] = Field(None)
    s3_uri_style: Optional[str] = Field(None, alias="s3-uri-style")
    tls_ca_chain: Optional[List[str]] = Field(None, alias="tls-ca-chain")

    @property
    def ca_cert(self) -> Optional[str]:
        """Unify the ca chain provided by the lib into a single cert."""
        return "\n\n".join(self.tls_ca_chain) if self.tls_ca_chain else None
