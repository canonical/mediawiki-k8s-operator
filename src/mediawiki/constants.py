# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared constants for the MediaWiki workload package.

Path and value constants used across the MediaWiki workload modules live here so
that they can be shared between the core class and the feature mixins without
duplication. Container paths are constructed from these string constants by the
module that owns them.
"""

# Users and groups
DAEMON_USER = "_daemon_"
DAEMON_GROUP = "_daemon_"
ROOT_USER_NAME = "root"
WEBROOT_OWNER_USER = "webroot_owner"

# Timeouts (seconds)
BASE_TIMEOUT = 60
LONG_TIMEOUT = BASE_TIMEOUT * 10
DB_CHECK_TIMEOUT = BASE_TIMEOUT * 3
DB_CHECK_INTERVAL = 5

# Number of times to attempt the MediaWiki installation script before giving up.
# The install can occasionally fail due to transient atomic operation issues.
INSTALL_MAX_ATTEMPTS = 3
# Short delay between installation attempts to let transient issues settle.
INSTALL_RETRY_INTERVAL = 2

# Extensions bundled in the rock image that should always be loaded
# during schema updates, regardless of whether they are configured.
BUNDLED_EXTENSIONS = ("PluggableAuth", "OpenIDConnect", "SimpleSAMLphp")

# Base directories
WEBROOT_PATH = "/var/www/html"
MEDIAWIKI_PATH = WEBROOT_PATH + "/w"
SECURE_SETTINGS_BASE_PATH = "/etc/mediawiki"
LOGS_PATH = "/var/log/mediawiki"

# Static assets
STATIC_ASSETS_MOUNT_POINT = "/mnt/static-assets"
STATIC_ASSETS_REPO_PATH = STATIC_ASSETS_MOUNT_POINT + "/repo"
WEBROOT_STATIC_PATH = WEBROOT_PATH + "/static"

# Composer
USER_COMPOSER_FILE = MEDIAWIKI_PATH + "/composer.user.json"
COMPOSER_LOCK_FILE = MEDIAWIKI_PATH + "/composer.lock"
COMPOSER_PATH = "/usr/bin/composer"

# Settings files
LOCAL_SETTINGS_FILE = MEDIAWIKI_PATH + "/LocalSettings.php"
USER_SETTINGS_FILE = SECURE_SETTINGS_BASE_PATH + "/UserSettings.php"
LATE_SETTINGS_FILE = SECURE_SETTINGS_BASE_PATH + "/LateSettings.php"
UPDATE_WRAPPER_FILE = SECURE_SETTINGS_BASE_PATH + "/UpdateWrapper.php"
JOB_RUNNER_CONFIG_PATH = SECURE_SETTINGS_BASE_PATH + "/JobRunnerConfig.json"

# Settings templates (relative to the src/templates directory)
LOCAL_SETTINGS_TEMPLATE = "LocalSettings.php.template"
LATE_SETTINGS_TEMPLATE = "LateSettings.php.template"

# Scripts
PHP_CLI_PATH = "/usr/bin/php"
MAINTENANCE_SCRIPTS_PATH = MEDIAWIKI_PATH + "/maintenance"

# robots.txt
ROBOTS_TXT_PATH = WEBROOT_PATH + "/robots.txt"

# webroot_owner SSH configuration
WEBROOT_OWNER_HOME = "/home/webroot_owner"
WEBROOT_OWNER_SSH_DIR = WEBROOT_OWNER_HOME + "/.ssh"
WEBROOT_OWNER_SSH_KEY = WEBROOT_OWNER_SSH_DIR + "/id_charm"
WEBROOT_OWNER_SSH_CONFIG = WEBROOT_OWNER_SSH_DIR + "/config"
WEBROOT_OWNER_KNOWN_HOSTS = WEBROOT_OWNER_SSH_DIR + "/known_hosts"

# SimpleSAMLphp (SAML authentication)
SIMPLESAMLPHP_CONFIG_DIR = "/etc/simplesamlphp"
