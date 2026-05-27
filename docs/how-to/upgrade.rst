.. meta::
   :description: How to upgrade the MediaWiki charm to a new revision.

.. _how_to_upgrade:

How to upgrade
==============

.. vale Canonical.004-Canonical-product-names = NO

To upgrade the MediaWiki charm to a new revision, use :doc:`juju refresh <juju:reference/juju-cli/list-of-juju-cli-commands/refresh>`:

.. vale Canonical.004-Canonical-product-names = YES

.. code-block:: bash

   juju refresh mediawiki-k8s

If you are upgrading to a new major release, you will need to update the database schema by running the ``update-database`` action on the leader unit.

.. vale Canonical.005-Industry-product-names = NO

.. warning::
    Updating the database schema is a potentially destructive operation. It is highly recommended to back up the database before performing a schema upgrade. Additional information can be found in the :doc:`MySQL documentation <mysql:how-to/back-up-and-restore/create-a-backup>`.

.. vale Canonical.005-Industry-product-names = YES

.. code-block:: bash

   juju run mediawiki-k8s/leader update-database
