.. meta::
   :description: Reference documentation for configurations settings in LocalSettings.php that the charm itself manages.

.. _reference_charm_managed_settings:

Charm-managed configuration settings
====================================

The ``LocalSettings.php`` file provides basic `configuration settings for MediaWiki <https://www.mediawiki.org/wiki/Manual:LocalSettings.php>`__. Though the MediaWiki charm allows for users to :ref:`configure their own settings <how_to_configure_mediawiki>`, some settings are managed by the charm itself to provide sensible defaults and to ensure proper operation and integration with other services.

.. list-table::
   :header-rows: 1
   :widths: auto

   * - Setting
     - Overridable
     - Notes
   * - `$wgAWSBucketName <https://www.mediawiki.org/wiki/Extension:AWS>`__
     - .. centered:: :bdg-warning:`Partially`
     - This is only set when the :ref:`s3-parameters relation <reference_relation_endpoints_s3_parameters>` is in use.
   * - `$wgAWSCredentials <https://www.mediawiki.org/wiki/Extension:AWS>`__
     - .. centered:: :bdg-warning:`Partially`
     - This is only set when the :ref:`s3-parameters relation <reference_relation_endpoints_s3_parameters>` is in use.
   * - `$wgAWSRegion <https://www.mediawiki.org/wiki/Extension:AWS>`__
     - .. centered:: :bdg-warning:`Partially`
     - This is only set when the :ref:`s3-parameters relation <reference_relation_endpoints_s3_parameters>` is in use.
   * - `$wgAllowSchemaUpdates <https://www.mediawiki.org/wiki/Manual:$wgAllowSchemaUpdates>`__
     - .. centered:: :bdg-danger:`No`
     - This is always set to ``false`` other than when the charm is performing a database update.
   * - `$wgArticlePath <https://www.mediawiki.org/wiki/Manual:$wgArticlePath>`__
     - .. centered:: :bdg-success:`Yes`
     - 
   * - `$wgDBname <https://www.mediawiki.org/wiki/Manual:$wgDBname>`__
     - .. centered:: :bdg-danger:`No`
     - This is based on the relation data from the :ref:`database relation <reference_relation_endpoints_database>`.
   * - `$wgDBservers <https://www.mediawiki.org/wiki/Manual:$wgDBservers>`__
     - .. centered:: :bdg-danger:`No`
     - This is based on the relation data from the :ref:`database relation <reference_relation_endpoints_database>`.
   * - `$wgDebugLogGroups <https://www.mediawiki.org/wiki/Manual:$wgDebugLogGroups>`__
     - .. centered:: :bdg-success:`Yes`
     - Logs written to ``/var/log/mediawiki/logs.log`` will be forwarded to Loki using the :ref:`logging relation <reference_relation_endpoints_logging>`.
   * - `$wgDiff3 <https://www.mediawiki.org/wiki/Manual:$wgDiff3>`__
     - .. centered:: :bdg-danger:`No`
     - 
   * - `$wgEnableUploads <https://www.mediawiki.org/wiki/Manual:$wgEnableUploads>`__
     - .. centered:: :bdg-warning:`Partially`
     - When the :ref:`s3-parameters relation <reference_relation_endpoints_s3_parameters>` is not in use, this setting is forced to be ``false`` to prevent uploads when S3-backed object storage is not available.
   * - `$wgFileBackends['s3']['endpoint']  <https://www.mediawiki.org/wiki/Manual:$wgFileBackends>`__
     - .. centered:: :bdg-warning:`Partially`
     - This is only set when the :ref:`s3-parameters relation <reference_relation_endpoints_s3_parameters>` is in use.
   * - `$wgFileBackends['s3']['use_path_style_endpoint'] <https://www.mediawiki.org/wiki/Manual:$wgFileBackends>`__
     - .. centered:: :bdg-warning:`Partially`
     - This is sometimes set when the :ref:`s3-parameters relation <reference_relation_endpoints_s3_parameters>` is in use.
   * - `$wgHTTPProxy <https://www.mediawiki.org/wiki/Manual:$wgHTTPProxy>`__
     - .. centered:: :bdg-danger:`No`
     - This is based on the model configuration key :ref:`juju-http-proxy <juju:model-config-juju-http-proxy>`.
   * - `$wgImageMagickConvertCommand <https://www.mediawiki.org/wiki/Manual:$wgImageMagickConvertCommand>`__
     - .. centered:: :bdg-danger:`No`
     - 
   * - `$wgInternalServer <https://www.mediawiki.org/wiki/Manual:$wgInternalServer>`__
     - .. centered:: :bdg-danger:`No`
     - 
   * - `$wgJobTypeConf['default'] <https://www.mediawiki.org/wiki/Manual:$wgJobTypeConf>`__
     - .. centered:: :bdg-warning:`Partially`
     - This is only set when the :ref:`redis relation <reference_relation_endpoints_redis>` is in use.
   * - `$wgLogos <https://www.mediawiki.org/wiki/Manual:$wgLogos>`__
     - .. centered:: :bdg-success:`Yes`
     - 
   * - `$wgMainCacheType <https://www.mediawiki.org/wiki/Manual:$wgMainCacheType>`__
     - .. centered:: :bdg-danger:`No`
     - 
   * - `$wgObjectCaches['redis'] <https://www.mediawiki.org/wiki/Manual:$wgObjectCaches>`__
     - .. centered:: :bdg-warning:`Partially`
     - This is only set when the :ref:`redis relation <reference_relation_endpoints_redis>` is in use.
   * - `$wgPluggableAuth_Config <https://www.mediawiki.org/wiki/Extension:PluggableAuth>`__
     - .. centered:: :bdg-warning:`Partially`
     - This is only set when the :ref:`oauth relation <reference_relation_endpoints_oauth>` is in use. However, the charm only configures ``plugin`` and ``data.providerURL``, ``data.clientID``, ``data.clientSecret``, ``data.scope``, and ``data.proxy`` of the first item. User modifications are otherwise merged in where possible.
   * - `$wgReadOnly <https://www.mediawiki.org/wiki/Manual:$wgReadOnly>`__
     - .. centered:: :bdg-warning:`Partially`
     - In certain situations such as when performing a database update, the charm may set this variable.
   * - `$wgResourceBasePath <https://www.mediawiki.org/wiki/Manual:$wgResourceBasePath>`__
     - .. centered:: :bdg-success:`Yes`
     - 
   * - `$wgScriptPath <https://www.mediawiki.org/wiki/Manual:$wgScriptPath>`__
     - .. centered:: :bdg-success:`Yes`
     - 
   * - `$wgSecretKey <https://www.mediawiki.org/wiki/Manual:$wgSecretKey>`__
     - .. centered:: :bdg-danger:`No`
     - This is generated by the charm. Refer to the :ref:`security overview <explanation_security>` for more information.
   * - `$wgServer <https://www.mediawiki.org/wiki/Manual:$wgServer>`__
     - .. centered:: :bdg-success:`Yes`
     - The default is based on the charm configuration key ``url-origin`` but it may still be overridden.
   * - `$wgSessionCacheType <https://www.mediawiki.org/wiki/Manual:$wgSessionCacheType>`__
     - .. centered:: :bdg-danger:`No`
     - 
   * - `$wgSessionSecret <https://www.mediawiki.org/wiki/Manual:$wgSessionSecret>`__
     - .. centered:: :bdg-danger:`No`
     - This is generated by the charm. Refer to the :ref:`security overview <explanation_security>` for more information.
   * - `$wgSitename <https://www.mediawiki.org/wiki/Manual:$wgSitename>`__
     - .. centered:: :bdg-success:`Yes`
     - 
