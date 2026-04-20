# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

terraform {
  required_version = "~> 1.12"
  required_providers {
    external = {
      version = "> 2"
      source  = "hashicorp/external"
    }
    juju = {
      version = "~> 1.0"
      source  = "juju/juju"
    }
  }
}

provider "juju" {}

variable "model_uuid" {
  type = string
}

resource "juju_application" "mysql" {
  model_uuid = var.model_uuid
  charm {
    channel = "8.4/edge"
    name    = "mysql-k8s"
  }

  config = {
    "profile" = "testing"
  }

  trust = true
}

resource "juju_integration" "database" {
  model_uuid = var.model_uuid

  application {
    name = "mediawiki-k8s"
  }

  application {
    name = juju_application.mysql.name
  }
}

# tflint-ignore: terraform_unused_declarations
data "external" "app_status" {
  program = ["bash", "${path.module}/wait-for-active.sh", var.model_uuid, "mediawiki-k8s", "3m"]

  depends_on = [
    juju_integration.database
  ]
}
