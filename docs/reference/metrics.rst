.. meta::
   :description: Reference documentation for the monitoring metrics provided by the MediaWiki charm.

.. _reference_metrics:

Metrics
=======

The MediaWiki charm exposes workload metrics in the `OpenMetrics format <https://github.com/OpenObservability/OpenMetrics/blob/main/specification/OpenMetrics.md#data-model>`_ through the :ref:`metrics-endpoint <reference_relation_endpoints_metrics_endpoint>` relation, which is part of the |COS| observability integration. Once the charm is integrated with a metrics consumer such as `prometheus-k8s <https://charmhub.io/prometheus-k8s>`_, the metrics described below are scraped and can be queried directly or visualized through the bundled :ref:`Grafana dashboard <reference_relation_endpoints_grafana_dashboard>`.

.. seealso::

   For more information about the Canonical Observability Stack and its components, refer to the `COS documentation <https://canonical.com/observability>`__.

Apache metrics
--------------

* **Source**: `Apache exporter <https://github.com/Lusitaniae/apache_exporter>`__
* **Container**: ``mediawiki``
* **Service**: ``apache-exporter``

The ``apache-exporter`` service runs continuously and collects metrics from the Apache ``/server-status`` endpoint, which is otherwise only reachable from within the same Kubernetes pod. The exporter publishes them under the ``apache_*`` prefix (for example: ``apache_accesses_total``, ``apache_workers``, ``apache_scoreboard``, ``apache_sent_kilobytes_total``, ``apache_cpuload``).

For the full list of exported metrics and their meanings, refer to the `Apache exporter documentation <https://github.com/Lusitaniae/apache_exporter#collectors>`__.

Git-sync metrics
----------------

* **Source**: `git-sync <https://github.com/kubernetes/git-sync>`_
* **Container**: ``git-sync``
* **Service**: ``git-sync``

The ``git-sync`` sidecar synchronizes extensions and skins from a remote Git repository. It is started with the ``--http-metrics`` flag so that it exposes its metrics under the ``git_sync_*`` prefix (for example: ``git_sync_count_total`` and ``git_sync_duration_seconds``), and reports the number and duration of synchronization operations by status.

For more information about the sidecar and its ``--http-metrics`` flag, refer to the `git-sync documentation <https://github.com/kubernetes/git-sync>`__.
