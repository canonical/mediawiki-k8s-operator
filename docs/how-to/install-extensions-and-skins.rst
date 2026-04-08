.. meta::
   :description: How to install additional extensions and skins

.. _how_to_install_extensions_and_skins:

How to install extensions and skins
===================================

MediaWiki and the MediaWiki charm :ref:`bundle a number of extensions and skins <reference_included_extensions_and_skins>` by default. However, you may want to install additional extensions and skins to further customize your MediaWiki deployment. This can be accomplished by leveraging `Composer <https://getcomposer.org/>`_, a dependency manager for PHP.

The MediaWiki charms allows you to configure an arbitrary ``composer.json`` file using ``juju config``, which is used by Composer to determine which packages to install.

For example, with a local ``composer.json`` file that specifies the extensions and skins you want to install, you can run the following command to update the charm's configuration:

.. code-block:: bash

   juju config mediawiki-k8s composer="$(cat ${PATH_TO_COMPOSER_FILE})"

When the ``composer`` configuration key is modified, the charm will run ``composer update`` to refresh the installed extensions and skins, as well as their dependencies.

.. note::

    Extensions and skins will *only* be refreshed when the ``composer`` configuration key is modified, or if a previous attempt failed.

To verify that your extensions or skin was installed correctly, enable it by :ref:`configuring the local settings <how_to_configure_mediawiki>`. Then, go to the ``Special:Version`` page on your MediaWiki instance to see if the extension or skin is listed.

.. seealso::

   Read more about the ``composer.json`` file schema from the official `Composer documentation <https://getcomposer.org/doc/04-schema.md>`__.
   
   Read more about how MediaWiki uses Composer to install MediaWiki extensions from the official `MediaWiki documentation <https://www.mediawiki.org/wiki/Composer/For_extensions>`__. 
