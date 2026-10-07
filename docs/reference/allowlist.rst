
.. meta::
   :description: Reference documentation for external services that the MediaWiki charm may need to connect to.

.. _reference_allowlist:

.. vale Canonical.007-Headings-sentence-case = NO

Allowlist
=====================================

.. vale Canonical.007-Headings-sentence-case = YES

This page lists destinations that you may need to add to a firewall allowlist to ensure that the MediaWiki K8s operator works properly.

Destinations to allow
---------------------

.. important::
   Depending on the source of any additional extensions and skins that you use, you may need to add other destinations to your firewall's allowlist.

   Optional MediaWiki features such as `InstantCommons <https://www.mediawiki.org/wiki/InstantCommons>`__ may also require additional destinations in your firewall's allowlist.

.. list-table::
   :header-rows: 1
   :widths: auto

   * - Destination
     - Protocol
     - Port
     - Description
   * - repo.packagist.org
     - HTTPS
     - 443
     - Extension installation
   * - gerrit.wikimedia.org
     - HTTPS
     - 443
     - Extension installation
   * - github.com
     - HTTPS
     - 443
     - Extension installation
   * - api.github.com
     - HTTPS
     - 443
     - Extension installation
   * - codeload.github.com
     - HTTPS
     - 443
     - Extension installation
   * - database.clamav.net
     - HTTPS
     - 443
     - ClamAV virus definition updates


Object storage
----------------

If you are using object storage for file uploads, ensure that your MediaWiki deployment can access the relevant object storage endpoints.
