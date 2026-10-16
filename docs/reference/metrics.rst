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

PHP-FPM metrics
---------------

* **Source**: `PHP-FPM status page <https://www.php.net/manual/en/fpm.status.php>`_
* **Container**: ``mediawiki``

The PHP-FPM status page is scraped in OpenMetrics format. Its listener is reachable over the
Kubernetes pod network and is not forwarded through Traefik; the status path remains denied on
the public HTTP and HTTPS listeners.

For details about the exported metrics, refer to the `PHP-FPM status page documentation
<https://www.php.net/manual/en/fpm.status.php>`__.

Shellbox metrics
----------------

* **Container**: ``shellbox``
* **Services**: ``apache-exporter``, ``apache2``

The Shellbox sidecar runs its own Apache and PHP-FPM. Its ``apache-exporter`` service publishes Apache metrics under the ``apache_*`` prefix, as described in `Apache metrics`_.

PHP-FPM metrics come from PHP-FPM's built-in status page. They are published under the ``phpfpm_*`` prefix (for example: ``phpfpm_accepted_connections``, ``phpfpm_active_processes``, ``phpfpm_listen_queue``, ``phpfpm_max_children_reached``, ``phpfpm_slow_requests``).

For the meaning of each value, refer to the `PHP-FPM status page documentation <https://www.php.net/manual/en/fpm.status.php>`__.
