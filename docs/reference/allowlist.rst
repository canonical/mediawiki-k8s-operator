
.. meta::
   :description: Reference documentation for external services that the MediaWiki charm may need to connect to.

.. _reference_allowlist:

.. vale Canonical.007-Headings-sentence-case = NO

Allowlist
=====================================

.. vale Canonical.007-Headings-sentence-case = YES

This page contains the domain URLs that you may need to add to a firewall allowlist to ensure that the MediaWiki K8s operator works properly.

Domain URLs to allow
--------------------

.. important::
   Depending on the source of any additional extensions and skins that you use, you may need to add other URLs to your firewall's allowlist.

.. list-table::
   :header-rows: 1
   :widths: auto

   * - Domain
     - Description
   * - https://repo.packagist.org/
     - Extensions
   * - https://gerrit.wikimedia.org/
     - Extensions
   * - https://api.github.com
     - Extensions
   * - https://codeload.github.com
     - Extensions
   * - https://database.clamav.net
     - ClamAV virus definition updates (regional mirrors under \*.clamav.net may also be used)


Object storage
----------------

If you are using object storage for file uploads, ensure that your MediaWiki deployment can access the relevant object storage endpoints.
