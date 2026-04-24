.. meta::
   :description: How to deploy and manage the MediaWiki charm using Terraform modules.

.. _how_to_terraform:

How to use Terraform
====================

The MediaWiki charm can be deployed and managed with `Terraform`_ using the `Terraform Juju provider`_.

To use the :ref:`MediaWiki Terraform module <reference_terraform>`, import it as follows:

.. code-block:: terraform

   data "juju_model" "my_model" {
      name = var.model
   }

   module "mediawiki_k8s" {
      source = "git::https://github.com/canonical/mediawiki-k8s-operator//terraform"
      
      model_uuid = data.juju_model.my_model.uuid
      # (Customize configuration variables here if needed)
   }

Then, to create integrations, add the following:

.. code-block:: terraform

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

..

   Review the full list of available integrations: :ref:`reference_relation_endpoints`
