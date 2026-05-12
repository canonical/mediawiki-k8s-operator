# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

output "application" {
  description = "Object representing the deployed application"
  value       = juju_application.mediawiki_k8s
}

output "provides" {
  description = "Provided endpoints"
  value = {
    metrics_endpoint = "metrics-endpoint"
  }
}

output "requires" {
  description = "Requires endpoints"
  value = {
    database      = "database"
    logging       = "logging"
    oauth         = "oauth"
    redis         = "redis"
    s3_parameters = "s3-parameters"
    traefik_route = "traefik-route"
  }
}
