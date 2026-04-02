.. meta::
   :description: Reference documentation for MediaWiki extensions and skins included with the MediaWiki charm.

.. _reference_included_extensions_and_skins:

Included extensions and skins
=============================

By default, MediaWiki includes `bundled extensions and skins <https://www.mediawiki.org/wiki/Bundled_extensions_and_skins>`_. The MediaWiki charm also installs additional extensions and skins at the OCI image build time in order to extend the charm's functionality.

Extensions and skins marked as "managed" are enabled and minimally configured by the MediaWiki operator based on charm configuration, integrations, and state. Where possible, user defined configurations through the charm's ``local-settings`` :ref:`configuration key <reference_configurations>` are merged with the charm operator's configuration.

.. list-table::
   :header-rows: 1
   :widths: auto

   * - Item
     - :abbr:`Managed (Enabled and configured by the operator)`
     - :abbr:`Description (Adopted from the extension or skin's documentation)`
   * - `AWS <https://www.mediawiki.org/wiki/Extension:AWS>`_
     - .. centered:: :bdg-success:`Yes`
     - Allows MediaWiki to use Amazon S3 (or any compatible API, such as Apache CloudStack or Digital Ocean Spaces) instead of the local ``images/`` directory to store a wiki's uploaded files. 
   * - `PluggableAuth <https://www.mediawiki.org/wiki/Extension:PluggableAuth>`_
     - .. centered:: :bdg-success:`Yes`
     - A framework for creating authentication and authorization extensions.
   * - `OpenID Connect <https://www.mediawiki.org/wiki/Extension:OpenID_Connect>`__
     - .. centered:: :bdg-success:`Yes`
     - Extends the PluggableAuth extension to provide authentication using `OpenID Connect <https://openid.net/connect/>`__.

.. seealso::

   Please refer to this `composer.json file <https://github.com/canonical/mediawiki-k8s-operator/blob/main/mediawiki_rock/files/var/www/html/w/composer.local.json>`_ for specifics on the extensions and skins that the MediaWiki charm installs in addition to what MediaWiki bundles.

