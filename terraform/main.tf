# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

resource "juju_application" "mediawiki_k8s" {
  name       = var.app_name
  model_uuid = var.model_uuid

  charm {
    name     = "mediawiki-k8s"
    channel  = var.channel
    revision = var.revision
  }

  config             = var.config
  constraints        = var.constraints
  units              = var.units
  storage_directives = var.storage_directives
  resources          = var.resources
}
