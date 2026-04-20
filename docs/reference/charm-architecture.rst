.. meta::
   :description: A technical overview of the MediaWiki charm's architecture, containers, and Pebble services.

.. _reference_charm_architecture:

Charm architecture
==================

.. vale Canonical.000-US-spellcheck = NO
.. vale Canonical.500-Repeated-words = NO

.. mermaid::
   :name: architecture-diagram

   flowchart LR
      subgraph UnitLeader["Unit (*Leader*)"]
         direction TB

         CharmContainerL["Container: Charm"]
         MediaWikiContainerL["Container: MediaWiki"]
         GitSyncContainerL["Container: git-sync"]
         AssetsL@{ shape: lin-cyl, label: "static-assets-repo<br/>(Filesystem storage)" }

         CharmContainerL -->|Control| MediaWikiContainerL
         CharmContainerL -->|Control| GitSyncContainerL
         GitSyncContainerL -.->|Sync to| AssetsL
         AssetsL -.->|Serve| MediaWikiContainerL
      end

      MediaWikiReplicaRelation@{ shape: bow-rect, label: "mediawiki-replica<br/>(Peer relation)" }
      ReplicaSecret@{ shape: tag-doc, label: "replica-secret<br/>(Application secret)" }

      subgraph Unit["Unit"]
         direction TB

         CharmContainer["Container: Charm"]
         MediaWikiContainer["Container: MediaWiki"]
         GitSyncContainer["Container: git-sync"]
         Assets@{ shape: lin-cyl, label: "static-assets-repo<br/>(Filesystem storage)" }

         CharmContainer -->|Control| MediaWikiContainer
         CharmContainer -->|Control| GitSyncContainer
         GitSyncContainer -.->|Sync to| Assets
         Assets -.->|Serve| MediaWikiContainer
      end

      UnitLeader <-.->MediaWikiReplicaRelation
      MediaWikiReplicaRelation <-.-> Unit

      UnitLeader <-.->|Read/Write| ReplicaSecret
      ReplicaSecret -.->|Read| Unit

.. vale Canonical.000-US-spellcheck = YES
.. vale Canonical.500-Repeated-words = YES

The MediaWiki K8s charm provides the core functionality of MediaWiki with a horizontally scalable architecture, all while maintaining user flexibility.

The charm design leverages the `sidecar <https://kubernetes.io/blog/2015/06/the-distributed-system-toolkit-patterns/#example-1-sidecar-containers>`_ pattern to allow multiple containers in each pod with :doc:`Pebble <juju:reference/pebble>` running as the workload container's entrypoint.

Pebble is a lightweight, API-driven process supervisor that is responsible for configuring processes to run in a container and controlling those processes throughout the workload lifecycle.

Pebble ``services`` are configured through `layers <https://github.com/canonical/pebble#layer-specification>`_, and the following containers each represent a layer forming the effective Pebble configuration, or ``plan``:

1. :ref:`MediaWiki <reference_charm_architecture_containers_mediawiki>`, which serves the MediaWiki application.
2. :ref:`GitSync <reference_charm_architecture_containers_git_sync>`, which handles optional synchronization of static assets from a git repository.

As a result, if you run ``kubectl get pods`` on a namespace named for the Juju model you've deployed the MediaWiki charm into, you'll see something like the following:

.. code-block:: bash

   NAME                             READY   STATUS    RESTARTS   AGE
   mediawiki-k8s-0                  3/3     Running   0         6h4m

This shows there are three containers: MediaWiki, git-sync, and the charm operator.

Containers
----------

The MediaWiki charm is driven by the :ref:`charm operator container <reference_charm_architecture_containers_charm>` that manages the workload sidecars. The core workload is the in the :ref:`MediaWiki container <reference_charm_architecture_containers_mediawiki>`. Supporting it is the :ref:`git-sync workload container <reference_charm_architecture_containers_git_sync>`, which can be optionally used for syncing static assets from a git repository. Both of these containers are managed by :doc:`Pebble <juju:reference/pebble>`.

.. vale Canonical.000-US-spellcheck = NO

.. mermaid::
   :name: containers-architecture-diagram

   flowchart TD
      subgraph MediaWikiContainer["Container: MediaWiki"]
         Pebble1["Pebble<br/>(Process supervisor)"]
         Apache@{ shape: subproc, label: "Apache Server<br/>(Serves MediaWiki)" }
         JobRunner@{ shape: subproc, label: "Job Runner<br/>(Background tasks)" }

         Pebble1 -->|Start/Manage| Apache
         Pebble1 -->|Start/Manage| JobRunner
      end
      
      subgraph GitSyncContainer["Container: git-sync"]
         Pebble2["Pebble<br/>(Process supervisor)"]
         GitSyncApp@{ shape: subproc, label: "git-sync<br/>(Repository sync)" }

         Pebble2 -->|Start/Manage| GitSyncApp
      end
      
      subgraph OperatorPlane["Container: Charm"]
         CharmOp["MediaWiki Operator<br/>(Lifecycle manager)"]
      end
      
      Assets@{ shape: lin-cyl, label: "static-assets-repo<br/>(Filesystem storage)" }
      
      CharmOp -->|Control| Pebble1
      CharmOp -->|Control| Pebble2
      GitSyncApp -.->|Sync to| Assets
      Assets -.->|Serve| Apache

.. vale Canonical.000-US-spellcheck = YES


.. _reference_charm_architecture_containers_mediawiki:

MediaWiki
^^^^^^^^^

The MediaWiki container consists of the main workload of the charm. Here, Pebble is used to run the Apache server which serves the MediaWiki application. Additionally, the ``static-assets-repo`` filesystem is mounted to this container in order to serve any static assets that may be synced by the :ref:`git-sync container <reference_charm_architecture_containers_git_sync>`.

Apache server
"""""""""""""

.. vale Canonical.025a-latinisms-with-english-equivalents = NO

The Apache server is configured by default to accept all web traffic on port 80, redirecting non-existing file and directory requests to the MediaWiki PHP index file with a ``RewriteRule`` directive. This enables the usage of `short URLs <https://www.mediawiki.org/wiki/Manual:Short_URL>`_ for the MediaWiki application. The Apache configuration files can be found in the |mediawiki_rock/files/etc/apache2|_ directory of the charm's source code.

.. vale Canonical.025a-latinisms-with-english-equivalents = YES

.. |mediawiki_rock/files/etc/apache2| replace:: ``mediawiki_rock/files/etc/apache2/``
.. _mediawiki_rock/files/etc/apache2: https://github.com/canonical/mediawiki-k8s-operator/tree/main/mediawiki_rock/files/etc/apache2

Job runner
"""""""""""

When Redis is used, Pebble runs a set of supporting `job runner services <https://github.com/wikimedia/mediawiki-services-jobrunner>`_ which performs MediaWiki's `long-running tasks asynchronously <https://www.mediawiki.org/wiki/Manual:Job_queue>`_. Without those services, these long-running tasks would otherwise only be run at the `end of a web request <https://www.mediawiki.org/wiki/Manual:$wgJobRunRate>`_.

MediaWiki application
"""""""""""""""""""""

MediaWiki is installed in the ``/w`` directory of the webroot.

To allow for users to :ref:`install arbitrary extensions and skins <how_to_install_extensions_and_skins>`, the ``composer.local.json`` file is configured to merge from an independent composer file which the charm operator injects based on the user charm configuration.

Similarly, the ``LocalSettings.php`` file is configured to allow inclusions from additional configuration files. This enables custom :ref:`user configurations <how_to_configure_mediawiki>` for the MediaWiki application, which is still also configured through a set of :ref:`charm managed settings <reference_charm_managed_settings>`. With this approach, the configuration files can be stored outside of the webroot for security hardening.

.. _reference_charm_architecture_containers_git_sync:

.. vale Canonical.007-Headings-sentence-case = NO

git-sync
^^^^^^^^

The ``git-sync`` sidecar container provides optional synchronization with a git repository. The repository is written to the ``static-assets-repo`` filesystem, which is mounted to both the git-sync and MediaWiki containers. This allows for users to sync arbitrary assets, such as images or CSS files, from a git repository to be served by the Apache server in the MediaWiki container.

.. vale Canonical.007-Headings-sentence-case = YES

.. _reference_charm_architecture_containers_charm:

Charm
^^^^^

This container is the main point-of-contact with the Juju controller. It communicates with Juju to run necessary charm code defined by the main ``src/charm.py``. The source code is copied to the ``/var/lib/juju/agents/unit-UNIT_NAME/charm`` directory.

OCI images
----------

We use :doc:`Rockcraft <rockcraft:index>` to build the OCI Image for the MediaWiki container.
The image is defined in |mediawiki_rock_link|_. Here, we include :ref:`a set of extensions and skins <reference_included_extensions_and_skins>`, in addition to the ones that MediaWiki bundles by default.

.. |mediawiki_rock_link| replace:: ``mediawiki_rock/``
.. _mediawiki_rock_link: https://github.com/canonical/mediawiki-k8s-operator/tree/main/mediawiki_rock

The ``git-sync`` container uses the `official git-sync image <https://github.com/kubernetes/git-sync>`_ with no modifications, other than the addition of Pebble.

Both of these OCI images are published to `Charmhub <https://charmhub.io/>`_, the official repository for charms.

Learn more: :ref:`How to publish your charm on Charmhub <charmcraft:publish-a-charm>`

..
   Metrics
   -------

   If the charm uses metrics, include a list under reference/metrics.md and link that document here.
   If the charm uses containers, you may include text here like:

   Inside the above mentioned containers, additional Pebble layers are defined in order to provide metrics.
   See (link-to-metrics-document) for more information.

Charm code overview
-------------------

The ``src/charm.py`` is the default entry point for a charm and has the ``Charm`` Python class which inherits
from CharmBase. CharmBase is the base class from which all charms are formed, defined
by `Ops <https://ops.readthedocs.io/en/latest/index.html>`_ (Python framework for developing charms).

   See more in the Juju docs: :doc:`Charm <juju:reference/charm>`

The ``__init__`` method guarantees that the charm observes all events relevant to its operation and handles them.

Take, for example, when a configuration is changed by using the CLI.

1. User runs the configuration command:

.. code-block:: bash

   juju config <relevant-charm-configuration>

2. A ``config-changed`` event is emitted.
3. In the ``__init__`` method is defined how to handle this event like this:

.. code-block:: python

   self.framework.observe(self.on.config_changed, self._reconciliation)

4. The method ``_reconciliation``, for its turn, will take the necessary actions such as waiting for all the relations to be ready and then configuring the containers.
