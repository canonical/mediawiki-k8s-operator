.. meta::
   :description: A step-by-step tutorial for deploying the MediaWiki charm for the first time.

.. _tutorial_basic_deployment:

Deploy the MediaWiki charm for the first time
==================================================

.. TODO: 1-2 sentences that introduce the charm and outlines what the tutorial will cover.
   For example, successfully deploying the charm and its required dependencies.

What you'll do
--------------

.. TODO: Add numbered list of steps outlining what happens in this tutorial.
   Example:

    1. Deploy the WordPress K8s charm
    2. Deploy and integrate a database
    3. Get admin credentials
    4. Access the WordPress instance
    5. Clean up the environment

What you'll need
----------------

.. vale Canonical.013-Spell-out-numbers-below-10 = NO

.. SPREAD SKIP

You will need a working station, e.g., a laptop, with AMD64 architecture. Your working station
should have at least 4 CPU cores, 8 GB of RAM, and 50 GB of disk space.

.. tip::

    You can use Multipass to create an isolated environment by running:

    .. code-block::

        multipass launch 24.04 --name charm-tutorial-vm --cpus 4 --memory 8G --disk 50G


This tutorial requires the following software to be installed on your working station
(either locally or in the Multipass VM):

.. TODO: Does this tutorial require a specific version of Juju?
         Does this tutorial require MicroK8s at all?
         If this is a machine charm, what version of LXD is required?

- Juju 3
- MicroK8s 1.33

Use `Concierge <https://github.com/canonical/concierge>`_ to set up Juju and MicroK8s:

.. code-block::

    sudo snap install --classic concierge
    sudo concierge prepare -p microk8s

.. TODO: If the tutorial requires a LXD controller, update "microk8s" to "machine"
         Double check that the text below is accurate!

This first command installs Concierge, and the second command uses Concierge to install
and configure Juju and MicroK8s.

For this tutorial, Juju must be bootstrapped to a MicroK8s controller. Concierge should
complete this step for you, and you can verify by checking for
``msg="Bootstrapped Juju" provider=microk8s``
in the terminal output and by running ``juju controllers``.

If Concierge did not perform the bootstrap, run:

.. code-block::

    juju bootstrap microk8s tutorial-controller


To be able to work inside the Multipass VM, log in with the following command:

.. code-block:: bash

    multipass shell charm-tutorial-vm 

.. note::

    If you're working locally, you don't need to do this step.

.. SPREAD SKIP END

Set up the environment
----------------------

To manage resources effectively and to separate this tutorial's workload from
your usual work, create a new model in the MicroK8s controller using the following command:

.. code-block::

    juju add-model wordpress-tutorial

Deploy the charm
----------------

.. TODO: Add instructions on deploying the charm

Deploy and integrate dependencies 
---------------------------------

.. TODO: If required, add instructions on deploying and integrating any other required charms
         Rename the section to something more specific (e.g., "Deploy and integrate database")


Run ``juju status`` to check the current status of the deployment.
The output should be similar to the following:

.. TODO: Add the output of juju status into a command block, showing a successful deployment. 
         If using the starter pack, use the terminal directive: https://github.com/canonical/sphinx-terminal/blob/main/README.md


When the status shows "Active" for both the charm, the deployment is considered finished.

Perform an action/configuration
-------------------------------

.. TODO: Provide instructions for running an action or updating a configuration.
         Choose a common task or operation. 
         Show any terminal output so the user can verify that their attempt was successful.

Clean up the environment
------------------------

.. TODO: Add a one-sentence summary about what the user accomplished in the tutorial.
         If using the starter pack, update the link to use intersphinx.

Congratulations! You successfully...

You can clean up your environment by following this guide:
:doc:`Tear down your test environment <juju:howto/manage-your-juju-deployment/tear-down-your-juju-deployment-local-testing-and-development>`

Next steps
----------

.. TODO: Fill in the list below with how-to guides or further reading about the charm.

You achieved a basic deployment of the charm. If you want to go farther in your deployment
or learn more about the charm, check out these pages:

- Continue with the advanced tutorial, which...
- Perform basic operations with your deployment like...
- Set up monitoring for your deployment by...
- Make your deployment more secure by...
- Learn more about the available :ref:`relation endpoints <reference_relation_endpoints>`
  for the charm.
