.. meta::
   :description: A high-level overview of the MediaWiki charm's deployment, including its relations to other charms.

.. _reference_high_level_deployment:

High-level overview of MediaWiki deployment
=================================================

The following diagram shows a typical, fully featured deployment of the MediaWiki charm on a Kubernetes cloud. The MediaWiki |K8s| model contains the core application and supporting charms, while external services such as MySQL, the :doc:`Canonical Observability Stack <observability:index>`, ingress, S3-compatible object storage, and an identity provider reside in separate Juju models or external infrastructure.

.. vale Canonical.000-US-spellcheck = NO
.. vale Canonical.005-Industry-product-names = NO
.. vale Canonical.500-Repeated-words = NO

.. mermaid::
   :name: deployment-diagram

   flowchart TB
      Users(["Users"])

      S3@{ shape: docs, label: "S3-compatible<br/>object storage"}

      subgraph ExternalIngress["Ingress model"]
         Ingress["haproxy"]
      end

      subgraph ExternalMySQL["MySQL model"]
         MySQL[("MySQL")]
      end

      subgraph ExternalCOS["COS model"]
         COS["Canonical<br/>Observability Stack"]
      end

      subgraph ExternalIdentity["Identity platform model"]
         Identity["Identity provider"]
      end

      subgraph K8sModel["MediaWiki K8s model"]
         Traefik["traefik-k8s"]
         IngressConfig["ingress-configurator"]
         MediaWiki["mediawiki-k8s"]
         MySQLRouter["mysql-router-k8s"]
         Redis["redis-k8s"]
         S3Int["s3-integrator"]
         OtelCol["opentelemetry-<br/>collector-k8s"]

         IngressConfig ---|"upstream-ingress<br/>(ingress)"| Traefik ---|"traefik-route<br/>(traefik_route)"| MediaWiki
         Redis ---|"redis"| MediaWiki
         MediaWiki ---|"database<br/>(mysql_client)"| MySQLRouter
         MediaWiki ---|"s3-parameters<br/>(s3)"| S3Int
         MediaWiki ----|"logging<br/>(loki_push_api)"| OtelCol
         MediaWiki ----|"grafana-dashboard<br/>(grafana_dashboard)"| OtelCol
         MediaWiki ----|"metrics-endpoint<br/>(prometheus_scrape)"| OtelCol
      end

      Users -.-|"HTTP/S"| ExternalIngress
      Ingress ---|"haproxy-route"| IngressConfig
      MySQLRouter ----|"backend-database<br/>(mysql_client)"| ExternalMySQL
      OtelCol --- ExternalCOS
      MediaWiki -----|"oauth"| ExternalIdentity
      S3Int -...-|"S3 API"| S3

.. vale Canonical.000-US-spellcheck = YES
.. vale Canonical.005-Industry-product-names = YES
.. vale Canonical.500-Repeated-words = YES

Components
----------

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Component
     - Role
   * - **mediawiki-k8s**
     - Core MediaWiki application charm
   * - **traefik-k8s**
     - Reverse proxy; routes HTTP traffic to MediaWiki
   * - **ingress-configurator**
     - Bridges Traefik to an external HAProxy deployment via ``haproxy-route``
   * - **mysql-router-k8s**
     - Routes database queries to an external MySQL cluster
   * - **redis-k8s**
     - Provides caching and asynchronous job execution
   * - **s3-integrator**
     - Supplies S3 credentials for user file uploads
   * - **opentelemetry-collector-k8s**
     - Forwards metrics, logs, and Grafana dashboards to |COS|
