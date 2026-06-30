.. meta::
   :description: A step-by-step tutorial for deploying the MediaWiki charm for the first time.

.. _tutorial_basic_deployment:

Deploy the MediaWiki charm for the first time
==================================================

The ``mediawiki-k8s`` charm can be used to deploy a horizontally-scalable MediaWiki application. This
tutorial will walk you through each step of deploying MediaWiki using the charm.

What you'll do
--------------

1. Deploy the `MediaWiki K8s charm`_
2. Deploy and integrate a database
3. Configure the URL origin
4. Get admin credentials
5. Access the MediaWiki instance
6. Clean up the environment

What you'll need
----------------

.. vale Canonical.013-Spell-out-numbers-below-10 = NO

.. SPREAD SKIP

You will need a workstation, for example, a laptop, with AMD64 architecture. Your workstation
should have at least 4 CPU cores, 8 GB of RAM, and 50 GB of disk space.

.. tip:: Using a Multipass VM (optional)

    You can use `Multipass`_ to create an isolated virtual machine (VM) by running:

    .. code-block::

        multipass launch 26.04 --name mediawiki-tutorial-vm --cpus 4 --memory 8G --disk 50G

    To be able to work inside the Multipass VM, open a shell with the following command:

    .. code-block:: bash

        multipass shell mediawiki-tutorial-vm

This tutorial requires the following software to be installed on your workstation
(either locally or in the Multipass VM):

- Juju 3.6
- MicroK8s 1.35

Use `Concierge <https://github.com/canonical/concierge>`_ to set up Juju and MicroK8s:

.. code-block::

    sudo snap install --classic concierge
    sudo concierge prepare -p microk8s

This first command installs Concierge, and the second command uses Concierge to install
and configure Juju and MicroK8s.

For this tutorial, Juju must be bootstrapped to a MicroK8s controller. Concierge should
complete this step for you, and you can verify by checking for
``msg="Bootstrapped Juju" provider=microk8s``
in the terminal output and by running ``juju controllers``.

If Concierge did not perform the bootstrap, run:

.. code-block::

    juju bootstrap microk8s tutorial-controller

.. SPREAD SKIP END

Set up the environment
----------------------

To manage resources effectively and to separate this tutorial's workload from
your usual work, create a new model in the MicroK8s controller using the following command:

.. code-block::

    juju add-model mediawiki-tutorial

Deploy the charm
----------------

Start by deploying the MediaWiki charm.

.. TODO: Update when we have something in stable.

.. code-block:: bash

    juju deploy mediawiki-k8s --channel 1.45/edge

Deploy and integrate database 
---------------------------------

MediaWiki requires a relational database. As the ``mysql_client`` :doc:`interface <juju:reference/relation>` is required by the ``mediawiki-k8s`` charm, we will use the `MySQL K8s charm <https://charmhub.io/mysql-k8s>`_ for this tutorial. 

Deploy the ``mysql-k8s`` charm and integrate it with ``mediawiki-k8s`` with the following:

.. code-block:: bash

    juju deploy mysql-k8s --trust --config profile=testing
    juju relate mediawiki-k8s mysql-k8s

Run ``juju status`` to check the current status of the deployment.
The output should be similar to the following:

.. terminal::
    :user: ubuntu
    :host: mediawiki-tutorial-vm

    juju status

    Model               Controller          Cloud/Region        Version  SLA          Timestamp
    mediawiki-tutorial  concierge-microk8s  microk8s/localhost  3.6.21   unsupported  18:43:02-04:00

    App            Version           Status  Scale  Charm          Channel     Rev  Address         Exposed  Message
    mediawiki-k8s  mediawiki-1.45.3  active      1  mediawiki-k8s  1.45/edge    17  10.152.183.157  no       
    mysql-k8s      8.0.44            active      1  mysql-k8s      8.0/stable  400  10.152.183.82   no       

    Unit              Workload  Agent  Address      Ports  Message
    mediawiki-k8s/0*  active    idle   10.1.153.77         
    mysql-k8s/0*      active    idle   10.1.153.82         Primary

When the status shows "Active" for both the MediaWiki and MySQL charms, the deployment is considered finished.

.. vale Canonical.007-Headings-sentence-case = NO

Configure URL origin
--------------------

.. vale Canonical.007-Headings-sentence-case = YES

For MediaWiki to work properly, it needs to know how users will access it. We can do this by configuring the URL origin. To keep things simple, we will use the IP of our sole ``mediawiki-k8s`` unit.

First, save the IP address of the MediaWiki charm unit in an environment variable:

.. code-block:: bash

    UNIT_IP=$(juju status --format json | jq -r '.applications."mediawiki-k8s".units."mediawiki-k8s/0".address')

Then set the URL origin configuration to the unit's IP:

.. code-block:: bash

    juju config mediawiki-k8s url-origin="http://${UNIT_IP}"

Access the MediaWiki application
--------------------------------

Now that we have an active deployment, let's access the MediaWiki application by accessing the IP of a mediawiki-k8s unit. To start managing MediaWiki as an administrator, you need to create an administrator account.

By running the ``create-and-promote`` action on a ``mediawiki-k8s`` unit, Juju will create the user, promote it to the requested groups, and return a generated password for you:

.. code-block:: bash

    juju run mediawiki-k8s/0 create-and-promote username=admin bureaucrat=true sysop=true generate-password=true

The result should be similar to the following, with the password value filled in:

.. terminal::
    :user: ubuntu
    :host: mediawiki-tutorial-vm

    juju run mediawiki-k8s/0 create-and-promote username=admin bureaucrat=true sysop=true generate-password=true

    Running operation 1 with 1 task
      - task 2 on unit-mediawiki-k8s-0

    Waiting for task 2...
    18:48:57 User 'admin' created and promoted successfully

    password: <password>
    username: admin

Now we can access MediaWiki in a browser at ``http://<UNIT_IP>``. Log in with the credentials retrieved from the action above.

.. note:: 
    If you are using a Multipass VM for this tutorial, you will need to route the IP from Multipass. To do this, first get the IP of the Multipass VM.
    Outside the Multipass VM run:

    .. code-block:: bash

        multipass info mediawiki-tutorial-vm

    The first IP from this command's output is the ``<VM_IP>``.

    Then route:

    .. code-block:: bash
   
        sudo ip route add <UNIT_IP> via <VM_IP>

Clean up the environment
------------------------

Congratulations! You successfully deployed the MediaWiki charm, added a database, customized it, and accessed the application.

.. vale Canonical.004-Canonical-product-names = NO

You can clean up your environment by following this guide:
:doc:`Tear down your test environment <juju:howto/manage-your-juju-deployment/tear-down-your-juju-deployment-local-testing-and-development>`

.. vale Canonical.004-Canonical-product-names = YES

Next steps
----------

You achieved a basic deployment of the MediaWiki charm. If you want to go further in your deployment
or learn more about the charm, check out these pages:

.. - Continue with the advanced tutorial, which...

- Perform basic operations with your deployment like :doc:`installing extensions and skins </how-to/install-extensions-and-skins>`.
- Set up monitoring for your deployment by :doc:`integrating with the Canonical Observability Stack (COS) </how-to/integrate-with-cos>`.
- Make your deployment more secure by learning more about the charm's security in the :doc:`security overview </explanation/security>` page.
- Learn more about the available :doc:`relation endpoints </reference/relation-endpoints>` for the MediaWiki charm.
