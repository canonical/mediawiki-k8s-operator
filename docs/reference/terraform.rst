.. meta::
   :description: Technical reference for the Terraform module available for deploying the MediaWiki charm.

.. _reference_terraform:

Terraform module
================

The `MediaWiki Terraform module`_ provides a convenient way to deploy and manage the MediaWiki charm using `Terraform`_. This module abstracts away the underlying Terraform configuration and allows users to easily integrate the MediaWiki charm with other services and applications in their deployment by using the `Terraform Juju provider`_.

Inputs
------

.. list-table::
   :header-rows: 1
   :widths: auto

   * - Name
     - Description
     - Type
     - Default
   * - ``app_name``
     - Name of the application in the Juju model.
     - ``string``
     - ``"mediawiki-k8s"``
   * - ``channel``
     - The channel to use when deploying a charm.
     - ``string``
     - ``"1.45/stable"``
   * - ``config``
     - Application config. See :ref:`charm configurations <reference_configurations>`.
     - ``map(string)``
     - ``{}``
   * - ``constraints``
     - Juju constraints to apply for this application.
     - ``string``
     - ``""``
   * - ``model_uuid``
     - Reference to an existing model resource or data source for the model to deploy to.
     - ``string``
     - ``""``
   * - ``revision``
     - Revision number of the charm.
     - ``number``
     - ``null``
   * - ``resources``
     - Map of resources used by the application.
     - ``map(string)``
     - ``{}``
   * - ``storage_directives``
     - Map of storage used by the application.
     - ``map(string)``
     - ``{}``
   * - ``units``
     - Number of units to deploy.
     - ``number``
     - ``1``

Outputs
-------

.. list-table::
   :header-rows: 1
   :widths: auto

   * - Name
     - Description
   * - ``application``
     - An object representing the deployed application.
   * - ``requires``
     - Map of the requires endpoints. See :ref:`relation endpoints <reference_relation_endpoints>`.
