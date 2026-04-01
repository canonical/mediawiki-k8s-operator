.. meta::
   :description: A technical overview of the MediaWiki charm's architecture, containers, and Pebble services.

.. _reference_charm_architecture:

Charm architecture
==================

.. Include a Mermaid diagram of the charm here. Include here if the diagram is not included in explanation/charm-design
   Limit the scope of this diagram to the charm only.
   How is the charm containerized? Include those separate pieces in this diagram.

Add overview material here:

1. What kind of application is it? What kind of software does it use?
2. Describe Pebble services.

.. Example text
   At its core, the MediaWiki charm is <software> that does <brief description>.

   The charm design leverages the `sidecar <https://kubernetes.io/blog/2015/06/the-distributed-system-toolkit-patterns/#example-1-sidecar-containers>`_ pattern to allow multiple containers in each pod with `Pebble <https://documentation.ubuntu.com/juju/3.6/reference/pebble/>`_ running as the workload container's entrypoint.

   Pebble is a lightweight, API-driven process supervisor that is responsible for configuring processes to run in a container and controlling those processes throughout the workload lifecycle.

   Pebble ``services`` are configured through `layers <https://github.com/canonical/pebble#layer-specification>`_, and the following containers represent each one a layer forming the effective Pebble configuration, or ``plan``:

   1. Container 1, which does this and that.
   2. Container 2, which does that and this.
   3. And so on.


   As a result, if you run a ``kubectl get pods`` on a namespace named for the Juju model you've deployed the MediaWiki charm into, you'll see something like the following:

   .. code-block:: bash

      NAME                             READY   STATUS    RESTARTS   AGE
      MediaWiki-0                   N/N     Running   0         6h4m

   This shows there are <NUMBER> containers - <describe what the containers are>.

Containers
----------

Configuration files for the containers can be found in the respective directories that define the rock.

..
   Container example
   ^^^^^^^^^^^^^^^^^

   Description of container.

   The workload that this container is running is defined in the (link to rock).

OCI images
----------

We use `Rockcraft <https://documentation.ubuntu.com/rockcraft/stable/>`_ to build OCI Images for MediaWiki.
The images are defined in (link to rock).
They are published to `Charmhub <https://charmhub.io/>`_, the official repository of charms.

See more: `How to publish your charm on Charmhub <https://documentation.ubuntu.com/charmcraft/stable/howto/manage-charms/#publish-a-charm-on-charmhub>`_

Metrics
-------

..
   If the charm uses metrics, include a list under reference/metrics.md and link that document here.
   If the charm uses containers, you may include text here like:

   Inside the above mentioned containers, additional Pebble layers are defined in order to provide metrics.
   See (link-to-metrics-document) for more information.

Charm code overview
-------------------

The ``src/charm.py`` is the default entry point for a charm and has the <relevant-charm-class> Python class which inherits
from CharmBase. CharmBase is the base class from which all charms are formed, defined
by `Ops <https://ops.readthedocs.io/en/latest/index.html>`_ (Python framework for developing charms).

   See more in the Juju docs: `Charm <https://documentation.ubuntu.com/juju/latest/user/reference/charm/>`_

The ``__init__`` method guarantees that the charm observes all events relevant to its operation and handles them.

Take, for example, when a configuration is changed by using the CLI.

1. User runs the configuration command:

.. code-block:: bash

   juju config <relevant-charm-configuration>

2. A ``config-changed`` event is emitted.
3. In the ``__init__`` method is defined how to handle this event like this:

.. code-block:: python

   self.framework.observe(self.on.config_changed, self._on_config_changed)

4. The method ``_on_config_changed``, for its turn, will take the necessary actions such as waiting for all the relations to be ready and then configuring the containers.
