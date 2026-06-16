# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Settings file generation for the MediaWiki workload.

Generates the PHP settings files (LocalSettings.php / LateSettings.php) as well as
the JobRunnerConfig.json used by the Redis-backed job queue runner, which runs
independently of the main MediaWiki application.
"""

from __future__ import annotations

import json
import logging
import textwrap
from typing import TYPE_CHECKING, Optional
from urllib.parse import urlparse

import charms.smtp_integrator.v0.smtp as smtp
from charmlibs.pathops import ContainerPath, LocalPath
from charms.saml_integrator.v0.saml import SamlRelationData

import utils
from exceptions import (
    MediaWikiBlockedStatusException,
    MediaWikiStatusException,
)
from mediawiki import constants
from mediawiki._base import _MediaWikiBase
from types_ import PhpTemplate

if TYPE_CHECKING:
    from mediawiki._secrets import MediaWikiSecrets
    from state import CharmConfig

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = LocalPath(__file__).parent.parent / "templates"


class _SettingsMixin(_MediaWikiBase):
    """Mixin providing MediaWiki settings file generation for :class:`MediaWiki`.

    Covers the PHP settings files (LocalSettings.php / LateSettings.php) and the
    JobRunnerConfig.json consumed by the Redis-backed job queue runner, which runs
    independently of the main MediaWiki application.
    """

    _local_settings_template_file = _TEMPLATES_DIR / constants.LOCAL_SETTINGS_TEMPLATE
    _late_settings_template_file = _TEMPLATES_DIR / constants.LATE_SETTINGS_TEMPLATE

    @property
    def _secure_settings_base_path(self) -> ContainerPath:
        """The base directory for charm-managed settings outside the webroot."""
        return ContainerPath(constants.SECURE_SETTINGS_BASE_PATH, container=self._container)

    @property
    def _late_settings_file(self) -> ContainerPath:
        """The LateSettings.php file managed by the charm."""
        return ContainerPath(constants.LATE_SETTINGS_FILE, container=self._container)

    def _settings_reconciliation(
        self,
        config: CharmConfig,
        secrets: MediaWikiSecrets,
        ro_database: bool = False,
    ) -> None:
        """Reconcile all the MediaWiki settings derived from LocalSettings.php.

        Args:
            config (CharmConfig): The charm configuration.
            secrets (MediaWikiSecrets): An instance of MediaWikiSecrets containing secrets synced between units.
            ro_database: Whether to include settings that put the database into read-only mode for updates. Defaults to False.

        Raises:
            MediaWikiBlockedStatusException: If S3 relation data is malformed (raised after settings are written).
        """
        self._secure_settings_base_path.mkdir(exist_ok=True, parents=True)

        self._push_user_settings(config)
        self._push_late_settings(secrets, ro_database=ro_database)
        self._push_local_settings(config)
        logger.debug("Settings reconciliation completed successfully.")

    def _push_user_settings(self, config: CharmConfig) -> None:
        """Push the user editable settings to the container."""
        self._user_settings_file.write_text(
            config.local_settings,
            mode=0o640,
            user=constants.ROOT_USER_NAME,
            group=constants.DAEMON_GROUP,
        )

    def _push_late_settings(self, secrets: MediaWikiSecrets, ro_database: bool = False) -> None:
        """Push the charm-controlled late MediaWiki settings to the container.

        Args:
            secrets (MediaWikiSecrets): An instance of MediaWikiSecrets containing secrets synced between units.
            ro_database: Whether to include settings that put the database into read-only mode for updates. Defaults to False.
        """
        self._secure_settings_base_path.mkdir(exist_ok=True, parents=True)
        content = self._late_settings_template_file.read_text()
        content += self._get_proxy_settings()
        content += self._get_database_settings()
        content += self._get_cache_settings()

        deferred_error: Optional[MediaWikiStatusException] = None

        try:
            content += self._get_auth_settings(secrets)
        except MediaWikiBlockedStatusException as e:
            logger.warning("Auth configuration incomplete: %s", e)
            deferred_error = e

        try:
            content += self._get_smtp_settings()
        except MediaWikiStatusException as e:
            logger.warning(
                "SMTP relation data is incomplete or malformed; disabling email notifications"
            )
            deferred_error = deferred_error or e
            content += "$wgEnableEmail = false;\n"

        try:
            content += self._get_s3_settings()
        except MediaWikiBlockedStatusException as e:
            logger.warning("S3 relation data is incomplete or malformed; disabling uploads")
            deferred_error = deferred_error or e
            content += "$wgEnableUploads = false;\n"

        if ro_database:
            # https://www.mediawiki.org/wiki/Manual:Upgrading#Can_my_wiki_stay_online_while_it_is_upgrading?
            content += "$adminTask = ( PHP_SAPI === 'cli' || defined( 'MEDIAWIKI_INSTALL' ) );\n"
            content += "$wgReadOnly = $adminTask ? false : 'Ongoing database update';\n"
        else:
            content += "$wgAllowSchemaUpdates = false;\n"

        for key, value in secrets.to_local_settings().items():
            content += f"{key} = '{utils.escape_php_string(value)}';\n"

        content += "?>\n"

        self._late_settings_file.write_text(
            content, mode=0o640, user=constants.ROOT_USER_NAME, group=constants.DAEMON_GROUP
        )

        # Raise any deferred configuration error after settings have been written to ensure
        # the config file is always in a consistent state
        if deferred_error:
            raise deferred_error

    def _push_local_settings(self, config: CharmConfig) -> None:
        """Push the base LocalSettings.php file to the container."""
        template = PhpTemplate(self._local_settings_template_file.read_text())
        server_name = config.url_origin or f"//{self._charm.app.name}"
        content = template.substitute(
            wg_server=f'"{utils.escape_php_string(server_name)}"',
        )
        content += textwrap.dedent(f"""
        require_once "{self._user_settings_file}";
        require_once "{self._late_settings_file}";
        ?>
        """)

        self._local_settings_file.write_text(
            content, mode=0o640, user=constants.WEBROOT_OWNER_USER, group=constants.DAEMON_GROUP
        )

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

        servers_php = [
            textwrap.dedent(f"""\
            [
                'host' => '{utils.escape_php_string(db_data.endpoints[0].to_string())}',
                'dbname' => '{utils.escape_php_string(db_data.database)}',
                'user' => '{utils.escape_php_string(db_data.username)}',
                'password' => '{utils.escape_php_string(db_data.password)}',
                'type' => 'mysql',
                'flags' => DBO_DEFAULT | DBO_SSL,
                'load' => 0,
            ]""")
        ]

        servers_str = ",\n".join(servers_php)
        _servers_idt = str.maketrans({"\n": "\n" + " " * 16})
        servers_str = servers_str.translate(_servers_idt)

        content = textwrap.dedent(
            f"""
            $wgDBname = '{utils.escape_php_string(db_data.database)}';
            $wgDBservers = [
                {servers_str}
            ];
            """
        )
        return content + "\n"

    def _get_cache_settings(self) -> str:
        """Get the current cache settings as a string, to be inserted into a PHP file.

        As a side effect, this also reconciles the JobRunnerConfig.json used by the
        Redis-backed job queue runner: it is written when Redis is available and
        removed otherwise. The job runner runs independently of the main MediaWiki
        application.

        Returns:
            str: The cache settings formatted as a PHP string.
        """
        if (not self._redis.is_relation_available()) or (
            not (endpoint := self._redis.get_endpoint())
        ):
            logger.debug(
                "Redis relation is not available or incomplete, using default cache settings."
            )
            self._job_runner_config.unlink(missing_ok=True)
            return (
                textwrap.dedent("""
                $wgMainCacheType = CACHE_NONE;
                $wgSessionCacheType = CACHE_DB;
                """)
                + "\n"
            )

        job_runner_config = {
            "groups": {
                "basic": {
                    "runners": 19,
                    "include": ["*"],
                    "low-priority": ["htmlCacheUpdate", "refreshLinks"],
                    "exclude": [
                        "AssembleUploadChunks",
                        "PublishStashedFile",
                        "uploadFromUrl",
                        "webVideoTranscode",
                        "webVideoTranscodePrioritized",
                    ],
                },
                "transcode": {"runners": 0, "include": ["webVideoTranscode"]},
                "priorityTranscode": {"runners": 0, "include": ["webVideoTranscodePrioritized"]},
                "upload": {
                    "runners": 7,
                    "include": ["AssembleUploadChunks", "PublishStashedFile", "uploadFromUrl"],
                },
            },
            "limits": {
                "attempts": {"*": 3},
                "claimTTL": {
                    "*": 3600,
                    "webVideoTranscode": 86400,
                    "webVideoTranscodePrioritized": 86400,
                },
                "real": {
                    "*": 300,
                    "webVideoTranscode": 86400,
                    "webVideoTranscodePrioritized": 86400,
                },
                "memory": {"*": "300M"},
            },
            "redis": {
                "aggregators": [endpoint],
                "queues": [endpoint],
            },
            "dispatcher": f"{self._php_cli_path} {self._maintenance_scripts_base_path / 'run.php'} runJobs --wiki=%(db)x --type=%(type)x --maxtime=%(maxtime)x --memory-limit=%(maxmem)x --result=json",
        }
        self._job_runner_config.write_text(
            json.dumps(job_runner_config, indent=4),
            mode=0o640,
            user=constants.ROOT_USER_NAME,
            group=constants.DAEMON_GROUP,
        )

        # https://www.mediawiki.org/wiki/Redis
        content = textwrap.dedent(
            f"""
            $wgObjectCaches['redis'] = [
                'class'                => 'RedisBagOStuff',
                'servers'              => [ '{utils.escape_php_string(endpoint)}' ],
            ];

            $wgMainCacheType = 'redis';
            $wgSessionCacheType = 'redis';

            $wgJobTypeConf['default'] = [
                'class'          => 'JobQueueRedis',
                'redisServer'    => '{utils.escape_php_string(endpoint)}',
                'redisConfig'    => [],
                'daemonized'     => true
            ];

            $wgJobRunRate = 0;
            """
        )
        return content + "\n"

    def _get_s3_settings(self) -> str:
        """Get the current S3 settings as a string, to be inserted into a PHP file.

        Note that even when S3 is available, uploads needs to explicitly enabled via LocalSettings.php.

        Returns:
            str: The S3 settings formatted as a PHP string.

        Raises:
            MediaWikiBlockedStatusException: If S3 relation data is incomplete or malformed.
        """
        if not self._s3.has_relation():
            return "$wgEnableUploads = false;\n"

        s3_data = self._s3.get_relation_data()

        # https://github.com/edwardspec/mediawiki-aws-s3
        # Note that $wgAWSRegion has to be set even if there is no region
        content = textwrap.dedent(
            f"""
            wfLoadExtension( 'AWS' );

            $wgAWSCredentials = [
                'key' => '{utils.escape_php_string(s3_data.access_key)}',
                'secret' => '{utils.escape_php_string(s3_data.secret_key)}',
                'token' => false
            ];
            $wgAWSRegion = '{utils.escape_php_string(s3_data.region or "eu-west-1")}';
            $wgAWSBucketName = '{utils.escape_php_string(s3_data.bucket)}';
            $wgFileBackends['s3']['endpoint'] = '{utils.escape_php_string(s3_data.endpoint)}';
            """
        )

        if s3_data.s3_uri_style and s3_data.s3_uri_style.lower() == "path":
            content += "$wgFileBackends['s3']['use_path_style_endpoint'] = true;\n"

        return content + "\n"

    def _get_smtp_settings(self) -> str:
        """Get the current SMTP settings as a string, to be inserted into a PHP file.

        Returns:
            str: The SMTP settings formatted as a PHP string.

        Raises:
            MediaWikiBlockedStatusException: If there is an error fetching the SMTP relation data.
            MediaWikiWaitingStatusException: If the SMTP relation is not yet available.
        """
        if not self._smtp.has_relation():
            return ""

        smtp_data = self._smtp.get_relation_data()

        host = (
            f"ssl://{smtp_data.host}"
            if smtp_data.transport_security == smtp.TransportSecurity.TLS
            else smtp_data.host
        )

        wg_smtp_entries = [
            f"'host' => '{utils.escape_php_string(host)}'",
            f"'port' => {smtp_data.port}",
            f"'auth' => {str(smtp_data.auth_type == smtp.AuthType.PLAIN).lower()}",
        ]

        if smtp_data.user is not None:
            wg_smtp_entries.append(f"'username' => '{utils.escape_php_string(smtp_data.user)}'")

        if smtp_data.password is not None:
            wg_smtp_entries.append(
                f"'password' => '{utils.escape_php_string(smtp_data.password)}'"
            )

        if smtp_data.skip_ssl_verify:
            # https://github.com/pear/Net_SMTP/blob/68420118ac8f9dfe5c4b8cac1bdb955efcd4be21/docs/guide.txt#id3
            wg_smtp_entries.append(
                "'socket_options' => array('ssl' => array('verify_peer' => false, 'verify_peer_name' => false))"
            )

        entries_str = textwrap.indent(
            textwrap.dedent("\n".join(f"{e}," for e in wg_smtp_entries)), " " * 4
        )
        content = textwrap.dedent(
            """\
            $wgSMTP = [
            {entries}
            ];
            """
        ).format(entries=entries_str)

        if smtp_data.smtp_sender is not None:
            content += f"$wgPasswordSender = '{utils.escape_php_string(smtp_data.smtp_sender)}';\n"

        return content + "\n"

    def _get_auth_settings(self, secrets: MediaWikiSecrets) -> str:
        """Get authentication extension settings (OAuth and/or SAML) as a PHP string.

        Orchestrates the shared PluggableAuth loading and delegates to helpers
        for provider-specific configuration.

        Returns:
            str: The combined auth settings formatted as a PHP string.
        """
        extensions: list[str] = []
        pluggable_auth_entries: list[str] = []

        oauth_entry = self._get_oauth_pluggable_auth_entry()
        if oauth_entry:
            extensions.append("OpenIDConnect")
            pluggable_auth_entries.append(oauth_entry)

        saml_entry = self._get_saml_pluggable_auth_entry(secrets)
        if saml_entry:
            extensions.append("SimpleSAMLphp")
            pluggable_auth_entries.append(saml_entry)

        if not pluggable_auth_entries:
            return ""

        load_lines = "\n                ".join(
            f"wfLoadExtension( '{ext}' );" for ext in ["PluggableAuth", *extensions]
        )
        _entry_indent = "                    "
        entries_str = (",\n" + _entry_indent).join(
            e.replace("\n", "\n" + _entry_indent) for e in pluggable_auth_entries
        )

        # The auth extensions should not be loaded when running createAndPromote.php.
        # See https://www.mediawiki.org/wiki/Extension:OpenID_Connect#Known_issues
        content = textwrap.dedent(
            f"""
            $_skipAuth = PHP_SAPI === 'cli'
                && in_array( 'createAndPromote', $_SERVER['argv'] ?? [], true );

            if ( !$_skipAuth ) {{
                {load_lines}
            }}
            unset( $_skipAuth );

            $wgPluggableAuth_Config = array_replace_recursive(
                $wgPluggableAuth_Config ?? [],
                [
                    {entries_str}
                ]
            );
            """
        )

        return content + "\n"

    def _get_oauth_pluggable_auth_entry(self) -> str | None:
        """Build the PluggableAuth config entry for OpenID Connect.

        Returns:
            The PHP array literal for the entry, or None if OAuth is not configured.
        """
        provider_info = self._oauth.get_provider_info()
        if (
            not provider_info
            or provider_info.client_id is None
            or provider_info.client_secret is None
        ):
            logger.debug("OAuth relation data is incomplete or missing, skipping OAuth settings.")
            return None

        # https://www.mediawiki.org/wiki/Extension:OpenID_Connect
        data_str = textwrap.dedent(f"""\
            'providerURL' => '{utils.escape_php_string(provider_info.issuer_url)}',
            'clientID' => '{utils.escape_php_string(provider_info.client_id)}',
            'clientSecret' => '{utils.escape_php_string(provider_info.client_secret)}'""")
        if provider_info.scope:
            scopes = self._oauth.scopes() & set(provider_info.scope.split())
            unsupported_scopes = self._oauth.scopes() - scopes
            if unsupported_scopes:
                logger.warning(
                    "OAuth provider does not support requested scopes: %s",
                    ", ".join(sorted(unsupported_scopes)),
                )
            data_str += f",\n'scope' => '{utils.escape_php_string(' '.join(sorted(scopes)))}'"
        if proxy := self._charm.state.proxy_config:
            if url := proxy.https_proxy_string:
                data_str += f",\n'proxy' => '{utils.escape_php_string(url)}'"
            elif url := proxy.http_proxy_string:
                logger.info("No HTTPS proxy; falling back to HTTP proxy for OIDC.")
                data_str += f",\n'proxy' => '{utils.escape_php_string(url)}'"

        _data_idt = str.maketrans({"\n": "\n" + " " * 8})
        data_str = data_str.translate(_data_idt)

        return textwrap.dedent(
            f"""\
            'OpenIDConnect' => [
                'plugin' => 'OpenIDConnect',
                'data' => [
                    {data_str}
                ]
            ]"""
        )

    def _get_saml_pluggable_auth_entry(self, secrets: MediaWikiSecrets) -> str | None:
        """Build the PluggableAuth config entry for SimpleSAMLphp.

        Also pushes the SimpleSAMLphp SP configuration files when SAML is active.

        Returns:
            The PHP array literal for the entry, or None if SAML is not configured.

        Raises:
            MediaWikiBlockedStatusException: If SAML is configured but Redis is not available.
        """
        saml_data = self._saml.get_relation_data()
        if not saml_data:
            logger.debug("SAML relation data is not available, skipping SAML settings.")
            return None

        if not saml_data.endpoints:
            logger.warning("SAML relation data has no endpoints, skipping SAML settings.")
            return None

        # Write SimpleSAMLphp SP configuration
        self._push_simplesamlphp_config(saml_data, secrets)

        return textwrap.dedent(
            """\
            'SimpleSAMLphp' => [
                'plugin' => 'SimpleSAMLphp',
                'data' => [
                    'authSourceId' => 'default-sp',
                ]
            ]"""
        )

    def _push_simplesamlphp_config(
        self,
        saml_data: SamlRelationData,
        secrets: MediaWikiSecrets,
    ) -> None:
        """Push SimpleSAMLphp SP configuration files to the container.

        Writes three files to the container:

        - ``authsources.php``: SP auth source pointing at the IdP entity ID.
        - ``charm-config.php``: charm-managed key overrides that are merged into the
          package's default ``config.php`` at runtime via a patch applied during the
          rock build. Contains the Redis session store, secret salt, base URL, and
          proxy settings.
        - ``metadata/saml20-idp-remote.php``: IdP metadata derived from the SAML
          relation (endpoints and certificates).

        If Redis is not available, or if the configured URL origin does not use HTTPS,
        ``charm-config.php`` is removed and a blocked status exception is raised before
        the file is written.

        Args:
            saml_data: The SAML relation data containing IdP information.
            secrets: The charm secrets, used to get the persistent SimpleSAMLphp secret salt.

        Raises:
            MediaWikiBlockedStatusException: If Redis is not available for session storage,
                or if the URL origin does not use HTTPS (required for secure session cookies).
        """
        config_dir = ContainerPath(constants.SIMPLESAMLPHP_CONFIG_DIR, container=self._container)
        metadata_dir = config_dir / "metadata"

        config_dir.mkdir(exist_ok=True, parents=True)
        metadata_dir.mkdir(exist_ok=True, parents=True)

        entity_id = utils.escape_php_string(saml_data.entity_id)

        # authsources.php
        authsources_content = textwrap.dedent(f"""\
            <?php
            $config = [
                'default-sp' => [
                    'saml:SP',
                    'idp' => '{entity_id}',
                ],
            ];
            """)
        (config_dir / "authsources.php").write_text(
            authsources_content,
            mode=0o640,
            user=constants.ROOT_USER_NAME,
            group=constants.DAEMON_GROUP,
        )

        # charm-config.php — charm-managed overrides merged into the package's config.php at
        # runtime via the include appended during the rock build. Depends on Redis.
        ssp_config_file = config_dir / "charm-config.php"
        redis_endpoint = self._redis.get_endpoint()
        if not redis_endpoint:
            ssp_config_file.unlink(missing_ok=True)
            raise MediaWikiBlockedStatusException(
                "SAML requires a Redis relation for SimpleSAMLphp session storage"
            )

        secret_salt = utils.escape_php_string(secrets.saml_secret_salt)
        redis_host, redis_port = redis_endpoint.rsplit(":", 1)
        charm_config = self._charm.load_charm_config()
        url_origin = charm_config.url_origin or f"https://{self._charm.app.name}"
        url_origin = urlparse(url_origin, scheme="https").geturl()
        baseurlpath = f"{url_origin}/w/simplesaml/"

        if urlparse(url_origin).scheme != "https":
            ssp_config_file.unlink(missing_ok=True)
            raise MediaWikiBlockedStatusException("HTTPS is required for SAML")

        config_entries: dict[str, str] = {
            "baseurlpath": f"'{utils.escape_php_string(baseurlpath)}'",
            "secretsalt": f"'{secret_salt}'",
            "application": f"[ 'baseURL' => '{utils.escape_php_string(url_origin)}' ]",
            "store.type": "'redis'",
            "store.redis.host": f"'{utils.escape_php_string(redis_host)}'",
            "store.redis.port": redis_port,
            "store.redis.prefix": "'SimpleSAMLphp'",
        }

        proxy_config = self._charm.state.proxy_config
        if proxy_config and proxy_config.https_proxy_string:
            config_entries["proxy"] = (
                f"'{utils.escape_php_string(proxy_config.https_proxy_string)}'"
            )

        entries_php = "\n".join(f"$config['{k}'] = {v};" for k, v in config_entries.items())
        config_content = f"<?php\n{entries_php}\n"
        ssp_config_file.write_text(
            config_content, mode=0o640, user=constants.ROOT_USER_NAME, group=constants.DAEMON_GROUP
        )

        # saml20-idp-remote.php — IdP metadata from the relation
        # Group endpoints by name (SingleSignOnService, SingleLogoutService)
        endpoints_by_name: dict[str, list] = {}
        for endpoint in saml_data.endpoints:
            endpoints_by_name.setdefault(endpoint.name, []).append(endpoint)

        idp_entries: list[str] = []

        for name, endpoints in endpoints_by_name.items():
            php_endpoints = []
            for ep in endpoints:
                parts = []
                if ep.url:
                    parts.append(f"'Location' => '{utils.escape_php_string(str(ep.url))}'")
                parts.append(f"'Binding' => '{utils.escape_php_string(ep.binding)}'")
                if ep.response_url:
                    parts.append(
                        f"'ResponseLocation' => '{utils.escape_php_string(str(ep.response_url))}'"
                    )
                php_endpoints.append("[" + ", ".join(parts) + "]")
            endpoints_str = ", ".join(php_endpoints)
            idp_entries.append(f"    '{name}' => [{endpoints_str}]")

        if saml_data.certificates:
            keys_php = ", ".join(
                f"['type' => 'X509Certificate', 'signing' => true,"
                f" 'encryption' => true,"
                f" 'X509Certificate' => '{utils.escape_php_string(c)}']"
                for c in saml_data.certificates
            )
            idp_entries.append(f"    'keys' => [{keys_php}]")

        entries_str = ",\n".join(idp_entries)
        idp_metadata_content = textwrap.dedent(f"""\
            <?php
            $metadata['{entity_id}'] = [
            {entries_str},
            ];
            """)
        (metadata_dir / "saml20-idp-remote.php").write_text(
            idp_metadata_content,
            mode=0o640,
            user=constants.ROOT_USER_NAME,
            group=constants.DAEMON_GROUP,
        )
