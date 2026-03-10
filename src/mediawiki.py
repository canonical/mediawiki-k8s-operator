# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Functions for managing and interacting with the primary MediaWiki workload/container."""

import dataclasses
import functools
import logging
import secrets
import textwrap
import time
from typing import Callable, List, TypeVar, Union, cast

import mysql.connector
import ops
import requests
from charmlibs.pathops import ContainerPath, LocalPath
from ops import Object

import utils
from database import Database
from exceptions import (
    MediaWikiBlockedStatusException,
    MediaWikiInstallError,
    MediaWikiWaitingStatusException,
)
from state import CharmConfig, StatefulCharmBase
from types_ import CommandExecResult, PhpTemplate

logger = logging.getLogger(__name__)

T = TypeVar("T")

INSTALLED_FLAG_TABLE = "mediawiki_charm_setup"


class MediaWiki(Object):
    """Class to manage MediaWiki."""

    _DAEMON_USER = "_daemon_"
    _DAEMON_GROUP = "_daemon_"
    _ROOT_USER_NAME = "root"
    _WEBROOT_OWNER_USER = "webroot_owner"

    _BASE_TIMEOUT = 60
    _LONG_TIMEOUT = _BASE_TIMEOUT * 10
    _DB_CHECK_TIMEOUT = _BASE_TIMEOUT * 3
    _REQUEST_TIMEOUT = 10
    _DB_CHECK_INTERVAL = 5

    # Template paths
    _local_settings_template_file = (
        LocalPath(__file__).parent / "templates" / "LocalSettings.php.template"
    )
    _late_settings_template_file = (
        LocalPath(__file__).parent / "templates" / "LateSettings.php.template"
    )

    def __init__(self, charm: StatefulCharmBase, database: Database):
        self._charm = charm
        self._container = self._charm.unit.get_container("mediawiki")
        self._database = database

        self._webroot_path = ContainerPath("/var/www/html", container=self._container)
        self._mediawiki_path = self._webroot_path / "w"

        self._robots_txt_path = self._webroot_path / "robots.txt"

        # Configuration paths
        self._user_composer_file = self._mediawiki_path / "composer.user.json"
        self._local_settings_file = self._mediawiki_path / "LocalSettings.php"

        ## Settings outside of Webroot
        self._secure_settings_base_path = ContainerPath(
            "/etc/mediawiki", container=self._container
        )
        self._user_settings_file = self._secure_settings_base_path / "UserSettings.php"
        self._late_settings_file = self._secure_settings_base_path / "LateSettings.php"

        # Script paths
        self._composer_path = ContainerPath("/usr/bin/composer", container=self._container)
        self._php_cli_path = ContainerPath("/usr/bin/php", container=self._container)
        self._ssh_keygen_path = ContainerPath("/usr/bin/ssh-keygen", container=self._container)
        self._maintenance_scripts_base_path = self._mediawiki_path / "maintenance"

        # webroot_owner SSH paths
        _webroot_owner_home = ContainerPath("/home/webroot_owner", container=self._container)
        self._webroot_owner_ssh_dir = _webroot_owner_home / ".ssh"
        self._webroot_owner_ssh_key = self._webroot_owner_ssh_dir / "id_ed25519"
        self._webroot_owner_ssh_config = self._webroot_owner_ssh_dir / "config"

    def reconciliation(self, secrets: "MediaWikiSecrets", ro_database: bool = False) -> None:
        """Reconcile the state of MediaWiki installation and configuration.

        The following actions are completed here:
        - Reconcile the SSH configuration for the webroot_owner user.
        - Reconcile the composer configuration, running composer update if needed.
        - Reconcile MediaWiki settings that are part of LocalSettings.php.
        - Reconcile the robots.txt file.
        - Install MediaWiki if the database is not initialized.

        Args:
            secrets: An instance of MediaWikiSecrets containing secrets synced between units.
            ro_database: Whether to include settings that put the database into read-only mode for updates. Defaults to False.

        Raises:
            MediaWikiStatusException: If there is a potentially transient error stopping the reconciliation process.
            MediaWikiInstallError: If there is an error during installation that should be investigated by an operator.
        """
        if not self._database.is_relation_ready():
            raise MediaWikiBlockedStatusException("Database relation is not ready")
        config = self._charm.load_charm_config()

        self._ssh_config_reconciliation()
        self._composer_reconciliation(config)
        self._settings_reconciliation(config, secrets, ro_database=ro_database)
        self._robots_txt_reconciliation(config)

        if not self._is_database_initialized():
            self._install()

    def rotate_root_credentials(self) -> tuple[str, str]:
        """Rotate the root bureaucrat user's credentials and ensure that it is in the bureaucrat group.
        If the user does not exist, it will be created.

        This user should only be used to assign permissions to real users, not for regular use.

        Returns:
            Tuple of (username, password) for the root user.

        Raises:
            MediaWikiInstallError: If there was an error creating or promoting the root user
        """
        root_password = secrets.token_urlsafe(64)
        result = self._run_maintenance_script(
            [
                "createAndPromote",
                "--bureaucrat",
                "--force",
                "--",
                self._ROOT_USER_NAME,
                root_password,
            ],
            sensitive=True,
        )
        if result.return_code != 0:
            logger.error(
                "Creating root user failed with return code %s\nstdout: %s\nstderr: %s",
                result.return_code,
                result.stdout,
                result.stderr,
            )
            raise MediaWikiInstallError("Creating root user failed; see logs for details.")
        else:
            logger.info("Root user creation output:\n%s", result.stdout)

        return self._ROOT_USER_NAME, root_password

    def update_database_schema(self) -> None:
        """Runs the update maintenance script, updating the MediaWiki database schema if needed.

        Should be ran after a MediaWiki upgrade, or after installing or updating an extension that requires a schema update.

        The database should be set to read only mode before running this method, and set back to read/write after completion.

        This is potentially dangerous action!

        Raises:
            MediaWikiInstallError: If the database update process fails.
        """
        result = self._run_maintenance_script(["update"])
        if result.return_code != 0:
            logger.error(
                "Database schema update failed with return code %s\nstdout: %s\nstderr: %s",
                result.return_code,
                result.stdout,
                result.stderr,
            )
            raise MediaWikiInstallError("Database schema update failed; see logs for details.")
        else:
            logger.info("Database schema update output:\n%s", result.stdout)

    def get_version(self) -> str:
        """Fetches the running version of MediaWiki via its API.

        Returns:
            The version string if it can be fetched, or an empty string if fetching the version failed for any reason.
        """
        try:
            response = requests.get(
                "http://localhost/w/api.php?action=query&format=json&prop=&meta=siteinfo&formatversion=2",
                timeout=self._REQUEST_TIMEOUT,
            )
        except requests.exceptions.RequestException as e:
            logger.error("Failed to fetch MediaWiki version: %s", e)
            return ""

        if response.status_code != 200:
            logger.error(
                "Failed to fetch MediaWiki version, API responded with status code %s",
                response.status_code,
            )
            return ""

        try:
            data = response.json()
            version = data.get("query", {}).get("general", {}).get("generator", "").lower()
            return "-".join(version.split()).lower()
        except requests.exceptions.JSONDecodeError:
            logger.error("Failed to decode MediaWiki version response as JSON: %s", response.text)
            return ""

    def _ssh_config_reconciliation(self) -> None:
        """Reconcile SSH configuration for the webroot_owner user.

        - Creates ~/.ssh/ with mode 700 if it does not exist.
        - Generates an ed25519 SSH key if one does not exist, allowing git clones
          from public repositories over SSH without manual key setup.
        - Writes ~/.ssh/config with StrictHostKeyChecking and a socat ProxyCommand
          if an HTTP proxy is configured.

        This allows tools like composer and git to clone over SSH (git@host: or
        git+ssh://) without interactive prompts, tunnelling through the proxy when
        one is present.

        Raises:
            MediaWikiInstallError: If the SSH key could not be generated.
        """
        self._webroot_owner_ssh_dir.mkdir(
            mode=0o700,
            parents=True,
            exist_ok=True,
            user=self._WEBROOT_OWNER_USER,
        )

        if not self._webroot_owner_ssh_key.exists():
            logger.info("Generating SSH key for %s.", self._WEBROOT_OWNER_USER)
            result = self._run_cli(
                [
                    str(self._ssh_keygen_path),
                    "-t",
                    "ed25519",
                    "-N",
                    "",  # No passphrase
                    "-f",
                    str(self._webroot_owner_ssh_key),
                ],
                user=self._WEBROOT_OWNER_USER,
            )
            if result.return_code != 0:
                logger.error(
                    "SSH key generation failed: return code %s\nstdout: %s\nstderr: %s",
                    result.return_code,
                    result.stdout,
                    result.stderr,
                )
                raise MediaWikiInstallError(
                    "Failed to generate SSH key for webroot_owner; see logs for details."
                )
            logger.info("SSH key generated for %s.", self._WEBROOT_OWNER_USER)

        ssh_config_lines = ["Host *", "    StrictHostKeyChecking accept-new"]
        if (proxy := self._charm.state.proxy_config) and proxy.http_proxy:
            proxy_host = str(proxy.http_proxy.host)
            proxy_port = str(proxy.http_proxy.port) if proxy.http_proxy.port else "3128"
            ssh_config_lines.append(
                f"    ProxyCommand socat - PROXY:{proxy_host}:%h:%p,proxyport={proxy_port}"
            )
        ssh_config = "\n".join(ssh_config_lines) + "\n"

        self._webroot_owner_ssh_config.write_text(
            ssh_config,
            mode=0o600,
            user=self._WEBROOT_OWNER_USER,
        )
        logger.debug("SSH configuration written for %s.", self._WEBROOT_OWNER_USER)

    def _composer_reconciliation(self, config: CharmConfig) -> None:
        """Reconcile the composer configuration, pushing the composer.user.json file if needed and running composer update.

        Args:
            config: The charm configuration.
        """
        current_composer = self._get_current_composer_json()

        # Only run if composer.json has changed or is missing
        if current_composer == config.composer:
            logger.debug("Composer configuration unchanged, skipping update.")
            return

        logger.info("Composer configuration changed or missing, running update.")

        self._user_composer_file.write_text(
            config.composer,
            mode=0o640,
            user=self._WEBROOT_OWNER_USER,
            group=self._DAEMON_GROUP,
        )

        composer_timeout = self._LONG_TIMEOUT * 2  # Composer update can take time
        proxy_env = (
            self._charm.state.proxy_config.as_dict if self._charm.state.proxy_config else {}
        )
        result = self._run_cli(
            [
                str(self._composer_path),
                "update",
                "--no-dev",
            ],
            user=self._WEBROOT_OWNER_USER,
            group=self._DAEMON_GROUP,
            working_dir=str(self._mediawiki_path),
            timeout=composer_timeout,
            environment=proxy_env,
        )

        if result.return_code != 0:
            logger.error(
                "Composer update failed with return code %s\nstdout: %s\nstderr: %s",
                result.return_code,
                result.stdout,
                result.stderr,
            )
            raise MediaWikiInstallError("Composer update failed; see logs for details.")

        logger.info("Composer update completed successfully: \n%s", result.stdout)

    def _get_current_composer_json(self) -> str:
        """Get the current content of composer.user.json."""
        if self._user_composer_file.exists():
            return self._user_composer_file.read_text().strip()
        return "{}"

    def _settings_reconciliation(
        self,
        config: CharmConfig,
        secrets: "MediaWikiSecrets",
        ro_database: bool = False,
    ) -> None:
        """Reconcile all the MediaWiki settings derived from LocalSettings.php.

        Args:
            config (CharmConfig): The charm configuration.
            secrets (MediaWikiSecrets): An instance of MediaWikiSecrets containing secrets synced between units.
            ro_database: Whether to include settings that put the database into read-only mode for updates. Defaults to False.
        """
        self._secure_settings_base_path.mkdir(exist_ok=True, parents=True)

        self._user_settings_file.write_text(
            config.local_settings, mode=0o640, user=self._ROOT_USER_NAME, group=self._DAEMON_GROUP
        )
        self._push_late_settings(secrets, ro_database=ro_database)
        self._push_local_settings(config)
        logger.debug("Settings reconciliation completed successfully.")

    def _push_late_settings(self, secrets: "MediaWikiSecrets", ro_database: bool = False) -> None:
        """Push the charm-controlled late MediaWiki settings to the container.

        Args:
            secrets (MediaWikiSecrets): An instance of MediaWikiSecrets containing secrets synced between units.
            ro_database: Whether to include settings that put the database into read-only mode for updates. Defaults to False.
        """
        self._secure_settings_base_path.mkdir(exist_ok=True, parents=True)
        content = self._late_settings_template_file.read_text()
        content += self._get_proxy_settings()
        content += self._get_database_settings()

        if ro_database:
            # https://www.mediawiki.org/wiki/Manual:Upgrading#Can_my_wiki_stay_online_while_it_is_upgrading?
            content += "$adminTask = ( PHP_SAPI === 'cli' || defined( 'MEDIAWIKI_INSTALL' ) );\n"
            content += "$wgReadOnly = $adminTask ? false : 'Ongoing database update';\n"
        else:
            content += "$wgAllowSchemaUpdates = false;\n"

        # Todo: Redis support
        content += "$wgMainCacheType = CACHE_NONE;\n"  # DB can be slower than None https://www.mediawiki.org/wiki/Manual:$wgMainCacheType
        content += "$wgSessionCacheType = CACHE_DB;\n"  # Sessions need to be guaranteed between units, DB is safer while we don't have Redis.

        for key, value in secrets.to_local_settings().items():
            content += f"{key} = '{utils.escape_php_string(value)}';\n"

        content += "?>\n"

        self._late_settings_file.write_text(
            content, mode=0o640, user=self._ROOT_USER_NAME, group=self._DAEMON_GROUP
        )

    def _push_local_settings(self, config: CharmConfig) -> None:
        """Push the base LocalSettings.php file to the container."""
        template = PhpTemplate(self._local_settings_template_file.read_text())
        server_name = config.hostname or self._charm.app.name
        content = template.substitute(
            wg_server=f'"//{utils.escape_php_string(server_name)}"',
        )
        content += textwrap.dedent(f"""
        require_once "{self._user_settings_file}";
        require_once "{self._late_settings_file}";
        ?>
        """)

        self._local_settings_file.write_text(
            content, mode=0o640, user=self._WEBROOT_OWNER_USER, group=self._DAEMON_GROUP
        )

    def _robots_txt_reconciliation(self, config: CharmConfig) -> None:
        """Push the robots.txt file to the container."""
        self._robots_txt_path.write_text(
            config.robots_txt, mode=0o640, user=self._ROOT_USER_NAME, group=self._DAEMON_GROUP
        )

    def _install(self) -> None:
        """Perform installation steps that should only be run by the leader unit.
        If the unit is not the leader, this method will wait until the database is marked as initialized by the leader, with a timeout.

        This includes running the MediaWiki installation script and creating a root user.
        The LocalSettings.php file must be in place before this method is called.
        """
        if not self._charm.unit.is_leader():
            logger.debug(
                f"Unit {self._charm.unit.name} is not leader; skipping leader-only installation steps."
            )
            self._charm.unit.status = ops.WaitingStatus(
                "Waiting for leader to perform installation"
            )

            deadline = time.time() + (self._DB_CHECK_TIMEOUT * 2)
            while time.time() < deadline:
                if self._is_database_initialized():
                    return
                time.sleep(self._DB_CHECK_INTERVAL)
            else:
                raise MediaWikiBlockedStatusException(
                    "Timed out waiting for leader to perform installation"
                )

        result = self._run_maintenance_script(["installPreConfigured"])
        if result.return_code != 0:
            logger.error(
                "MediaWiki installation failed with return code %s\nstdout: %s\nstderr: %s",
                result.return_code,
                result.stdout,
                result.stderr,
            )
            raise MediaWikiInstallError("MediaWiki installation failed; see logs for details.")
        else:
            logger.info("MediaWiki installation script output:\n%s", result.stdout)
        logger.info("Completed MediaWiki install script")

        self.rotate_root_credentials()
        logger.info("Completed root user creation.")

        self._set_database_initialized()

        logger.info("Completed MediaWiki installation.")

    def _get_proxy_settings(self) -> str:
        """Get the current proxy settings as a string, to be inserted into a PHP file."""
        wg_http_proxy = ""

        if (proxy := self._charm.state.proxy_config) and (url := proxy.http_proxy_string):
            wg_http_proxy = f"$wgHttpProxy = '{utils.escape_php_string(url)}';\n"

        return wg_http_proxy

    def _get_database_settings(self) -> str:
        """Get the current database settings as a string, to be inserted into a PHP file.

        Returns:
            str: The database settings formatted as a PHP string.

        Raises:
            MediaWikiWaitingStatusException: If the database relation is not ready.
            MediaWikiBlockedStatusException: If the database relation is in a blocked state.
        """
        db_data = self._database.get_relation_data()

        # Todo: DB SSL using self-signed certs support
        servers_php = [
            textwrap.dedent(f"""\
            [
                'host' => '{utils.escape_php_string(db_data.endpoints[0].to_string())}',
                'dbname' => '{utils.escape_php_string(db_data.database)}',
                'user' => '{utils.escape_php_string(db_data.username)}',
                'password' => '{utils.escape_php_string(db_data.password)}',
                'type' => 'mysql',
                'flags' => DBO_DEFAULT,
                'load' => 0,
            ]""")
        ]

        servers_str = ",\n".join(servers_php)
        servers_str = textwrap.indent(servers_str, "    ")

        content = textwrap.dedent(
            f"""
            $wgDBname = '{utils.escape_php_string(db_data.database)}';
            $wgDBservers = [
                {servers_str}
            ];
            """
        )
        return content + "\n"

    @staticmethod
    def _db_retry_deco(func: Callable[..., T]) -> Callable[..., T]:
        """Decorator to retry a database operation with a timeout."""

        @functools.wraps(func)
        def wrapper(self: "MediaWiki") -> T:
            deadline = time.time() + self._DB_CHECK_TIMEOUT
            while time.time() < deadline:
                try:
                    return func(self)
                except (mysql.connector.Error, MediaWikiWaitingStatusException) as e:
                    logger.warning("Database operation failed with error: %s", e)
                    time.sleep(self._DB_CHECK_INTERVAL)
            else:
                raise MediaWikiBlockedStatusException("MySQL database operation failed")

        return wrapper

    @_db_retry_deco
    def _set_database_initialized(self) -> None:
        """Mark the MediaWiki database as initialized by creating a flag table."""
        with self._database.get_database_connection() as cnx:
            try:
                cursor = cnx.cursor()
                # Should be safe since INSTALLED_FLAG_TABLE is a constant.
                cursor.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {INSTALLED_FLAG_TABLE} (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        installed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
                cnx.commit()
                logger.debug("Marked database as initialized")
            except Exception as e:
                cnx.rollback()
                raise e

    @_db_retry_deco
    def _is_database_initialized(self) -> bool:
        """Check if the MediaWiki database has been initialized by a charm."""
        with self._database.get_database_connection() as cnx:
            cursor = cnx.cursor()
            # Should be safe since INSTALLED_FLAG_TABLE is a constant.
            cursor.execute(f"SHOW TABLES LIKE '{INSTALLED_FLAG_TABLE}'")
            result = cursor.fetchone()
            if result:
                return True
        return False

    def _run_maintenance_script(
        self,
        args: List[str],
        timeout: int = _LONG_TIMEOUT,
        combine_stderr: bool = False,
        sensitive: bool = False,
    ) -> CommandExecResult:
        """Execute a MediaWiki maintenance script with the given arguments.

        This is a helper method for running maintenance scripts in the form of "php maintenance/run.php <args>".

        If timeout is exceeded, an MediaWikiInstallError will be raised.
        """
        result = self._run_cli(
            [str(self._php_cli_path), str(self._maintenance_scripts_base_path / "run.php"), *args],
            user=self._DAEMON_USER,
            group=self._DAEMON_GROUP,
            timeout=timeout,
            combine_stderr=combine_stderr,
            sensitive=sensitive,
        )
        return result

    def _run_cli(
        self,
        cmd: List[str],
        *,
        environment: dict[str, str] | None = None,
        user: Union[str, None] = None,
        group: Union[str, None] = None,
        working_dir: Union[str, None] = None,
        combine_stderr: bool = False,
        timeout: int = _BASE_TIMEOUT,
        sensitive: bool = False,
    ) -> CommandExecResult:
        """Execute a command in MediaWiki container.

        Args:
            cmd (List[str]): The command to be executed.
            environment (dict[str, str], optional): Environment variables to set for the command. Defaults to None.
            user (str): Username to run this command as, use root when not provided.
            group (str): Name of the group to run this command as, use root when not provided.
            working_dir (str):  Working dir to run this command in, use home dir if not provided.
            combine_stderr (bool): Redirect stderr to stdout, when enabled, stderr in the result
                will always be empty.
            timeout (int): Set a timeout for the running program in seconds.
                ``MediaWikiInstallError`` will be raised if timeout exceeded.
            sensitive (bool): Whether the command contains sensitive information, such as passwords. If True, the command will be redacted in logs.

        Returns:
            A named tuple with three fields: return code, stdout and stderr. Stdout and stderr are
            both string.
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

            raise MediaWikiInstallError(
                "Container command execution timed out; see logs for details."
            )

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


@dataclasses.dataclass(frozen=True)
class MediaWikiSecrets:
    """A dataclass to hold secrets relevant to MediaWiki that need to be synced between units."""

    secret_key: str
    session_secret: str

    @classmethod
    def generate(cls) -> "MediaWikiSecrets":
        """Returns a new instance of MediaWikiSecrets with randomly generated secrets."""
        return cls(
            secret_key=secrets.token_urlsafe(64),
            session_secret=secrets.token_urlsafe(64),
        )

    def to_local_settings(self) -> dict[str, str]:
        """Return the secrets formatted as a dictionary of PHP variable assignments to be included in LateSettings.php."""
        return {
            "$wgSecretKey": self.secret_key,
            "$wgSessionSecret": self.session_secret,
        }

    def to_juju_secret(self) -> dict[str, str]:
        """Return the secrets formatted as a dictionary for storing in Juju secrets."""
        # Juju secrets restricts key names to lowercase alphanumerics and dashes.
        return {
            "key": self.secret_key,
            "session": self.session_secret,
        }

    @classmethod
    def from_juju_secret(cls, data: dict[str, str]) -> "MediaWikiSecrets":
        """Create an instance of MediaWikiSecrets from a Juju secret style dictionary."""
        return cls(
            secret_key=data["key"],
            session_secret=data["session"],
        )
