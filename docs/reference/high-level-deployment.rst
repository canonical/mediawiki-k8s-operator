.. meta::
   :description: A high-level overview of the MediaWiki charm's deployment, including its relations to other charms.

.. _reference_high_level_deployment:

High-level overview of MediaWiki deployment
=================================================

The following diagram shows a typical, fully featured deployment of the MediaWiki charm on a Kubernetes cloud. The MediaWiki |K8s| model contains the core application and supporting charms, while external services such as MySQL, the :doc:`Canonical Observability Stack <observability:index>`, ingress, S3-compatible object storage, an identity provider, and SMTP relay infrastructure reside in separate Juju models or external infrastructure.

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

      subgraph ExternalSMTP["Mail infrastructure"]
        SMTPRelay["SMTP relay"]
      end

      subgraph K8sModel["MediaWiki K8s model"]
         Traefik["traefik-k8s"]
         IngressConfig["ingress-configurator"]
         MediaWiki["mediawiki-k8s"]
         MySQLRouter["mysql-router-k8s"]
         Valkey["valkey"]
         Certs["self-signed-certificates"]
         S3Int["s3-integrator"]
         SAMLInt["saml-integrator"]
         SMTPInt["smtp-integrator"]
         OtelCol["opentelemetry-<br/>collector-k8s"]

         IngressConfig ---|"upstream-ingress<br/>(ingress)"| Traefik ---|"traefik-route<br/>(traefik_route)"| MediaWiki
         Certs ---|"certificates<br/>(tls-certificates)"| Traefik
         Certs ---|"send-ca-cert to receive-ca-cert<br/>(certificate_transfer)"| Traefik
         Certs ---|"certificates<br/>(tls-certificates)"| MediaWiki
         Certs ---|"certificates to client-certificates<br/>(tls-certificates)"| Valkey
        Valkey ---|"valkey<br/>(valkey_client)"| MediaWiki
         MediaWiki ---|"database<br/>(mysql_client)"| MySQLRouter
         MediaWiki ---|"s3-parameters<br/>(s3)"| S3Int
         MediaWiki ---|"saml<br/>(saml)"| SAMLInt
         MediaWiki ---|"smtp<br/>(smtp)"| SMTPInt
         MediaWiki ----|"logging<br/>(loki_push_api)"| OtelCol
         MediaWiki ----|"grafana-dashboard<br/>(grafana_dashboard)"| OtelCol
         MediaWiki ----|"metrics-endpoint<br/>(prometheus_scrape)"| OtelCol
      end

      Users -.-|"HTTP/S"| ExternalIngress
      Ingress ---|"haproxy-route"| IngressConfig
      Ingress ---|"send-ca-cert to receive-ca-certs<br/>(certificate_transfer)"| Certs
      MySQLRouter ----|"backend-database<br/>(mysql_client)"| ExternalMySQL
      OtelCol --- ExternalCOS
      SAMLInt -...-|"IdP configuration"| ExternalIdentity
      S3Int -...-|"S3 API"| S3
      SMTPInt -...-|"SMTP"| ExternalSMTP

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
   * - **valkey**
     - Provides caching and asynchronous job execution
   * - **self-signed-certificates**
     - Issues TLS certificates for MediaWiki, Valkey, and Traefik, and distributes its CA certificate to Traefik and HAProxy
   * - **s3-integrator**
     - Supplies S3 credentials for user file uploads
   * - **saml-integrator**
     - Supplies identity provider configuration to MediaWiki for SAML sign-on
   * - **smtp-integrator**
     - Supplies SMTP relay configuration for outgoing emails
   * - **opentelemetry-collector-k8s**
     - Forwards metrics, logs, and Grafana dashboards to |COS|
