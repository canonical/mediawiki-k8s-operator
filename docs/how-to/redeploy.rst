.. meta::
    :description: Redeploy the MediaWiki charm and migrate data when replacing supporting services.

.. _how_to_redeploy:

How to redeploy
===============

While the MediaWiki charm itself is stateless, a full redeployment involving data migration requires special steps to minimize disruption to users and ensure data integrity.

.. warning::
    Redeploying the MediaWiki charm regenerates its charm-managed secrets. This may invalidate existing user sessions.

.. tip::
    If you only wish to upgrade the MediaWiki charm and don't need to migrate data to a new deployment, consider :doc:`refreshing the charm <upgrade>` instead.

Preparation
-----------

Depending on your risk tolerance and operational requirements, there are two methods to prepare for a redeployment:

- **Offline:** take the wiki offline to minimize the risk of data inconsistency. Users cannot access the site during redeployment.

- **Read-only:** keep the wiki available for reading while restricting writes. This method does not guarantee data consistency, and some MediaWiki features may not work during redeployment.

Offline method
^^^^^^^^^^^^^^

Take the wiki offline and stop the MediaWiki processes by removing the relations between MediaWiki and its supporting charms:

.. code-block:: bash

    juju remove-relation mediawiki-k8s traefik-k8s:traefik_route
    juju remove-relation mediawiki-k8s mysql-k8s:database
    juju remove-relation mediawiki-k8s valkey:valkey-client

Wait for all units of the MediaWiki charm to settle into a blocked state by checking their status with ``juju status --relations``.

Verify that the site is no longer accessible before continuing.

Read-only method
^^^^^^^^^^^^^^^^

Enable read-only maintenance mode to let users continue reading the wiki while restricting writes:

.. warning::
    Read-only maintenance mode also restricts operations that users might not expect to require writes, such as logging out.

    While maintenance mode pauses the job queue and restricts writes from MediaWiki, maintenance mode does not guarantee a complete freeze of Valkey data.

.. code-block:: bash

    juju run mediawiki-k8s/leader set-maintenance-mode \
        enabled=true \
        message="Site is undergoing scheduled maintenance."

Wait for all units to settle by checking their status with ``juju status``. Verify that the site now rejects attempts to edit a page before continuing.

Migrate data
------------

Data migration needs to occur before redeploying and reintegrating the MediaWiki charm.

Follow the data migration documentation for the :doc:`MySQL charm <mysql:how-to/data-migration/index>` and for the :doc:`Valkey charm <valkey:index>`.

If image uploads are enabled, you can reuse the existing S3 bucket. If you are replacing the bucket, migrate its contents at this stage as well.

Redeploy MediaWiki
------------------

Redeploy the MediaWiki charm with your required configuration and integrations, connecting it to the retained or replacement supporting services.
Charm-managed secrets are regenerated during redeployment, so you do not need to rotate them manually as part of this procedure.

Wait for all units to finish processing the changes and check their status with ``juju status``.

If you used the **read-only** preparation method, disable maintenance mode on the replacement deployment to allow database writes again:

.. code-block:: bash

    juju run mediawiki-k8s/leader set-maintenance-mode \
        enabled=false

.. note::

    For either preparation method, if you also upgraded to a new major MediaWiki release, you may need to update the database schema. If you used the read-only method, disable maintenance mode before running this action:

    .. code-block:: bash

        juju run mediawiki-k8s/leader update-database
