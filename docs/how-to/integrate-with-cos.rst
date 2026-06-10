.. meta::
   :description: How to integrate the MediaWiki charm with the Canonical Observability Stack (COS).

.. _how_to_integrate_with_cos:

How to integrate with COS
=========================

The MediaWiki charm comes with built-in support for integration with the :doc:`Canonical Observability Stack (COS) <observability:index>`. This guide describes the process of integrating with |COS| to monitor the MediaWiki charm.

Requirements
-------------

1. Deploy the MediaWiki charm. For steps on how to do this, see the :doc:`basic deployment tutorial</tutorial/basic-deployment>`.
2. Deploy |COS| in an independent model. For steps on how to do this, see the :doc:`deploying the observability stack tutorials <observability:tutorial/installation/index>`.

Steps
-----

.. vale Canonical.007-Headings-sentence-case = NO

Deploy the OpenTelemetry Collector charm
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. vale Canonical.007-Headings-sentence-case = YES

Deploy the `OpenTelemetry Collector K8s charm <https://charmhub.io/opentelemetry-collector-k8s>`_ in the same model as the MediaWiki charm.

.. code-block:: bash

   juju deploy opentelemetry-collector-k8s --channel 2/stable

.. vale Canonical.007-Headings-sentence-case = NO

Integrate the OpenTelemetry Collector with COS
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. vale Canonical.007-Headings-sentence-case = YES

Next, integrate the OpenTelemetry Collector charm with |COS| by relating it to the offers that |COS| provides. The appropriate offer names differ depending on whether you are using COS or COS Lite.

.. vale Canonical.004-Canonical-product-names = NO

The following examples assume that you deployed |COS| in a model named ``cos``, with the user ``admin``, and with the same controller that you deployed the MediaWiki charm with. If that is not the case, please :doc:`adjust the commands accordingly <juju:reference/juju-cli/list-of-juju-cli-commands/integrate>`.

.. vale Canonical.004-Canonical-product-names = YES

.. tabs::
   .. tab:: COS

      .. code-block:: bash

         juju integrate opentelemetry-collector-k8s:grafana-dashboards-provider admin/cos.grafana-dashboards
         juju integrate opentelemetry-collector-k8s:send-loki-logs admin/cos.loki-logging
         juju integrate opentelemetry-collector-k8s:send-remote-write admin/cos.mimir-receive-remote-write

   .. tab:: COS Lite

      .. code-block:: bash

         juju integrate opentelemetry-collector-k8s:grafana-dashboards-provider admin/cos.grafana-dashboards
         juju integrate opentelemetry-collector-k8s:send-loki-logs admin/cos.loki-logging
         juju integrate opentelemetry-collector-k8s:send-remote-write admin/cos.prometheus-receive-remote-write

.. vale Canonical.007-Headings-sentence-case = NO

Integrate MediaWiki with the OpenTelemetry Collector
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. vale Canonical.007-Headings-sentence-case = YES

Finally, relate the MediaWiki charm to the OpenTelemetry Collector charm to start sending metrics and logs to |COS|.

.. code-block:: bash

   juju integrate mediawiki-k8s:grafana-dashboard opentelemetry-collector-k8s:grafana-dashboards-consumer
   juju integrate mediawiki-k8s:logging opentelemetry-collector-k8s:receive-loki-logs
   juju integrate mediawiki-k8s:metrics-endpoint opentelemetry-collector-k8s:metrics-endpoint

You should now be able to access a Grafana dashboard named ``MediaWiki Operator Overview`` in the Grafana instance that |COS| provides.

.. seealso::

   For detailed information about the specific metrics that the MediaWiki charm provides, refer to the :doc:`metrics reference </reference/metrics>`.
