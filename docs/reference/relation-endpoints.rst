.. meta::
   :description: Reference documentation for all relation endpoints supported by the MediaWiki charm.

.. _reference_relation_endpoints:

Relation endpoints
==================

.. _reference_relation_endpoints_database:

Database
--------

.. vale Canonical.500-Repeated-words = NO
.. vale Canonical.005-Industry-product-names = NO
.. vale Canonical.000-US-spellcheck = NO

* **Interface**: `mysql_client <https://charmhub.io/integrations/mysql_client>`_
* **Supported charms**: `mysql-k8s <https://charmhub.io/mysql-k8s>`_, `mysql <https://charmhub.io/mysql>`_, `mysql-router-k8s <https://charmhub.io/mysql-router-k8s>`_

.. vale Canonical.500-Repeated-words = YES
.. vale Canonical.005-Industry-product-names = YES
.. vale Canonical.000-US-spellcheck = NO

The ``database`` relation endpoint is a **mandatory** relation that shares connection information for a MySQL database with MediaWiki, providing storage for MediaWiki.
You may choose to directly connect to a MySQL charm, or use a MySQL router charm to connect to an existing MySQL cluster.

Example ``database`` integrate command:

.. code-block:: bash

   juju integrate mediawiki-k8s mysql-k8s:database

.. _reference_relation_endpoints_oauth:

OAuth
-----

* **Interface**: `oauth <https://charmhub.io/integrations/oauth>`_
* **Supported charms**: `hydra <https://charmhub.io/hydra>`_

.. warning::
   While the ``oauth`` relation will function while configuring MediaWiki to use a HTTP or protocol-relative URL, it is **highly** recommended to explicitly allow only HTTPS in a production environment. 

The ``oauth`` relation connects with an OAuth provider to allow for easy setup of :abbr:`OAuth (Open Authorization)` based :abbr:`SSO (Single Sign-On)`.

This is accomplished using the `OpenID Connect MediaWiki extension <https://www.mediawiki.org/wiki/Extension:OpenID_Connect>`_, which is included with the MediaWiki charm.
The OpenID Connect extension can be further configured through the ``oauth-extra-scopes`` and ``local-settings`` :ref:`configuration options <reference_configurations>`.

Example ``oauth`` integrate command:

.. code-block:: bash

   juju integrate mediawiki-k8s hydra:oauth

.. seealso::

   Read more about the `Canonical Identity Platform <https://canonical-identity.readthedocs-hosted.com>`_.

.. _reference_relation_endpoints_redis:

Redis
-----

* **Interface**: `redis <https://charmhub.io/integrations/redis>`_
* **Supported charms**: `redis-k8s <https://charmhub.io/redis-k8s>`_

The ``redis`` relation connects MediaWiki to a Redis instance, allowing for caching of MediaWiki data in Redis. This can improve the performance of your MediaWiki instance.

Example ``redis`` integrate command:

.. code-block:: bash

   juju integrate mediawiki-k8s redis-k8s:redis

.. seealso::

   Read more about how MediaWiki uses Redis as an object cache backend: `Redis <https://www.mediawiki.org/wiki/Redis>`__

.. _reference_relation_endpoints_s3_parameters:

S3 parameters
-------------

* **Interface**: `s3 <https://charmhub.io/integrations/s3>`_
* **Supported charms**: `s3-integrator <https://charmhub.io/s3-integrator>`_

The ``s3-parameters`` relation endpoint provides MediaWiki with the necessary information to upload files to an S3-compatible object storage service, allowing for user uploads of files. This relation is only required if you wish to allow file uploads in your MediaWiki instance.

The MediaWiki charm uses the included `AWS MediaWiki extension <https://www.mediawiki.org/wiki/Extension:AWS>`_ for this functionality.

.. important::
   For security reasons and separation of load, the MediaWiki charm will not act as a reverse proxy for the S3 compatible object storage service. You need to separately ensure that your users can reach and read from your configured object storage service.

   To configure which endpoint MediaWiki will redirect users to in order to serve file uploads, configure the ``$wgAWSBucketDomain`` parameter in the ``local-settings`` :ref:`configuration option <reference_configurations>`.

   Refer to the `AWS MediaWiki extension documentation <https://www.mediawiki.org/wiki/Extension:AWS#Configuration>`_ for more information on how to configure the AWS extension.

Example ``s3-parameters`` integrate command:

.. code-block:: bash

   juju integrate mediawiki-k8s s3-integrator:s3-credentials

Traefik route
-------------

* **Interface**: `traefik_route <https://charmhub.io/integrations/traefik_route>`_
* **Supported charms**: `traefik-k8s <https://charmhub.io/traefik-k8s>`_

The ``traefik_route`` relation allows MediaWiki to connect to a Traefik charm deployment to automatically configure routing from outside the Kubernetes cluster to MediaWiki.

Example ``traefik_route`` integrate command:

.. code-block:: bash

   juju integrate mediawiki-k8s traefik-k8s:traefik_route
