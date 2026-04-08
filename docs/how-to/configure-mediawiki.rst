.. meta::
   :description: How to configure MediaWiki using LocalSettings.php.

.. _how_to_configure_mediawiki:

How to configure MediaWiki
==========================

MediaWiki's basic configuration settings are managed using the ``LocalSettings.php`` file. While the MediaWiki charm will provide some sensible defaults and also generate some necessary secrets, you may want to customize the configuration of your MediaWiki deployment by providing your own ``LocalSettings.php`` file.

.. warning::

   Certain configuration settings, such as database credentials and secrets like ``$wgSecretKey``, are :ref:`handled entirely by the MediaWiki charm <reference_charm_managed_settings>`. The settings are applied in such a way that the Charm's management has greater priority over most manually configured settings.

The MediaWiki charm allows you to configure an arbitrary ``LocalSettings.php`` file using ``juju config``.

For example, with a local ``LocalSettings.php`` file, you can run the following command to update the charm's configuration:

.. code-block:: bash

   juju config mediawiki-k8s local-settings="$(cat ${PATH_TO_LOCAL_SETTINGS_FILE})"

.. note::

   The user-configured ``LocalSettings.php`` contents are stored outside the webroot and are not world-readable. This is to minimize the risk of exposing sensitive information. Refer to the :ref:`security overview <explanation_security>` for more details.

.. seealso::

   Read more about the ``LocalSettings.php`` file from the official `MediaWiki documentation <https://www.mediawiki.org/wiki/Manual:LocalSettings.php>`__.
