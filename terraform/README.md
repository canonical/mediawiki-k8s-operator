# MediaWiki Terraform module

This folder contains a base [Terraform][Terraform] module for the MediaWiki charm.

The module uses the [Terraform Juju provider][Terraform Juju provider] to model the charm
deployment onto any Kubernetes environment managed by [Juju][Juju].

## Module structure

- **main.tf** - Defines the Juju application to be deployed.
- **variables.tf** - Allows customization of the deployment. Also models the charm configuration,
  except for exposing the deployment options (Juju model name, channel or application name).
- **outputs.tf** - Integrates the module with other Terraform modules, primarily
  by defining potential integration endpoints (charm integrations), but also by exposing
  the Juju application name.
- **versions.tf** - Defines the Terraform provider version.

## Using mediawiki_k8s base module in higher level modules

If you want to use `mediawiki_k8s` base module as part of your Terraform module, import it
like shown below:

```text
data "juju_model" "my_model" {
  name = var.model
}

module "mediawiki_k8s" {
  source = "git::https://github.com/canonical/mediawiki-k8s-operator//terraform"
  
  model_uuid = data.juju_model.my_model.uuid
  # (Customize configuration variables here if needed)
}
```

Create integrations, for instance:

```text
resource "juju_integration" "mediawiki-mysql" {
  model_uuid = data.juju_model.my_model.uuid
  application {
    name     = module.mediawiki_k8s.application.name
    endpoint = module.mediawiki_k8s.requires.database
  }
  application {
    name     = "mysql-k8s"
    endpoint = "database"
  }
}
```

The complete list of available integrations can be found [in the Integrations tab][MediaWiki-integrations].

[Terraform]: https://developer.hashicorp.com/terraform
[Terraform Juju provider]: https://registry.terraform.io/providers/juju/juju/latest
[Juju]: https://juju.is
[MediaWiki-integrations]: https://charmhub.io/mediawiki-k8s/integrations
