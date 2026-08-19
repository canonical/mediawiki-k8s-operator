.. meta::
   :description: Troubleshoot common issues with the MediaWiki charm and its deployment.

.. _how-to-troubleshoot:

How to troubleshoot
===================

This troubleshooting page provides guidance on Juju statuses, logs, and direct access of the containers.

Check status
------------

.. vale Canonical.004-Canonical-product-names = NO

Checking the statuses and messages of all applications and units using :doc:`juju status <juju:reference/juju-cli/list-of-juju-cli-commands/status>` should be your first troubleshooting step.

.. dropdown:: Expand to view a sample ``juju status`` output

   .. terminal::
      :user: ubuntu
      :host: mediawiki-tutorial-vm

      juju status

      Model               Controller          Cloud/Region        Version  SLA          Timestamp
      mediawiki-tutorial  concierge-microk8s  microk8s/localhost  3.6.21   unsupported  18:43:02-04:00

      App            Version           Status  Scale  Charm          Channel     Rev  Address         Exposed  Message
      mediawiki-k8s  mediawiki-1.46.0  active      1  mediawiki-k8s  1.46/edge   102  10.152.183.157  no       
      mysql-k8s      8.0.44            active      1  mysql-k8s      8.0/stable  400  10.152.183.82   no       

      Unit              Workload  Agent  Address      Ports  Message
      mediawiki-k8s/0*  active    idle   10.1.153.77         
      mysql-k8s/0*      active    idle   10.1.153.82         Primary

.. vale Canonical.004-Canonical-product-names = YES

Check Juju logs
---------------

.. vale Canonical.004-Canonical-product-names = NO

Before doing additional troubleshooting, check the Juju logs using :doc:`juju debug-log <juju:reference/juju-cli/list-of-juju-cli-commands/debug-log>`:

.. vale Canonical.004-Canonical-product-names = YES

.. code-block:: bash

   juju debug-log --replay --tail

To focus on ``ERRORS``:

.. code-block:: bash

   juju debug-log --replay | grep ERROR

Consider enabling the ``DEBUG`` level logs if you are troubleshooting unexpected charm behavior:

.. code-block:: bash

   juju model-config 'logging-config=<root>=INFO;unit=DEBUG'

Check Kubernetes pods
---------------------

.. admonition:: Charm architecture

   It may be helpful to familiarize yourself with the MediaWiki charm's :ref:`architecture <reference_charm_architecture>` before troubleshooting the pods.

To view a summary of all Kubernetes pods associated with a Juju model:

.. code-block:: bash

   kubectl get pods -n <model-name>

.. dropdown:: Expand to view a sample ``kubectl get pods`` output

   .. terminal::
      :user: ubuntu
      :host: mediawiki-tutorial-vm

      kubectl get pods -n mediawiki-tutorial

      NAME                             READY   STATUS    RESTARTS       AGE
      ingress-configurator-0           1/1     Running   0              1d
      mediawiki-0                      3/3     Running   0              1d
      mediawiki-1                      3/3     Running   0              1d
      mediawiki-2                      3/3     Running   0              1d
      modeloperator-5668865b78-s99zw   1/1     Running   0              1d
      otelcol-0                        2/2     Running   0              1d
      redis-0                          3/3     Running   0              1d
      s3-integrator-0                  1/1     Running   0              1d
      saml-integrator-0                1/1     Running   0              1d
      self-signed-certificates-0       1/1     Running   0              1d
      traefik-0                        2/2     Running   0              1d

To see more details on a running Kubernetes pod, use:

.. code-block:: bash

   kubectl describe pod <application>-<unit-ID> -n <model-name>

.. dropdown:: Expand to view a sample ``kubectl describe pod`` output

   .. terminal::
      :user: ubuntu
      :host: mediawiki-tutorial-vm

      kubectl describe pod mediawiki-k8s-0 -n mediawiki-tutorial

      ...
      Containers:
         charm:
            ...
            State:          Running
            Ready:          True
            Restart Count:  0
            ...
         git-sync:
            ...
            State:          Running
            Ready:          True
            Restart Count:  0
            ...
         mediawiki:
            ...
            State:          Running
            Ready:          True
            Restart Count:  0
            ...
      Volumes:
         mediawiki-static-assets-repo-68f59519:
            Type:       PersistentVolumeClaim (a reference to a PersistentVolumeClaim in the same namespace)
            ...
         charm-data:
            Type:       EmptyDir (a temporary directory that shares a pod's lifetime)
            ...

Check containers and services
-----------------------------

If the previous troubleshooting steps were not sufficient, you can directly access the :ref:`containers <reference_charm_architecture_containers>` containing the workloads and Juju agent.

``charm`` container
^^^^^^^^^^^^^^^^^^^

.. _how-to-troubleshoot_accessing_containers_charm_container:

To enter the ``charm`` container, use:

.. code-block:: bash
   
   juju ssh <application>/<unit-ID>

From here, you can check to make sure Pebble is running:

.. terminal::
   :user: root
   :host: mediawiki-k8s-0

   ps auxww

   USER         PID %CPU %MEM    VSZ   RSS TTY      STAT START   TIME COMMAND
   root           1  0.0  0.0 1237444 15392 ?       Ssl  Jul23  20:08 /charm/bin/pebble run --http :38812 --verbose
   root          17  0.0  0.4 1310432 74340 ?       Sl   Jul23  18:05 /charm/bin/containeragent unit --data-dir /var/lib/juju --append-env PATH=$PATH:/charm/bin --show-log --charm-modified-version 1
   ...

You can also check that the container agent is active:

.. code-block:: bash

   /charm/bin/pebble services

.. dropdown:: Expand to view a sample ``pebble services`` output from the ``charm`` container

   .. terminal::
      :user: root
      :host: mediawiki-k8s-0

      /charm/bin/pebble services

      Service          Startup  Current  Since
      container-agent  enabled  active   today at 13:49 UTC

``git-sync`` container
^^^^^^^^^^^^^^^^^^^^^^

To enter the ``git-sync`` container, use:

.. code-block:: bash
   
   juju ssh --container git-sync <application>/<unit-ID>

From here, you can check the state of the ``git-sync`` Pebble service:

.. code-block:: bash

   /charm/bin/pebble services

.. dropdown:: Expand to view a sample ``pebble services`` output from the ``git-sync`` container

   .. terminal::
      :user: root
      :host: mediawiki-k8s-0

      /charm/bin/pebble services

      Service          Startup  Current  Since
      git-sync  enabled  active   today at 13:50 UTC

You can also view the ``git-sync`` logs:

.. code-block:: bash

   /charm/bin/pebble logs git-sync --follow -n=all

``mediawiki`` container
^^^^^^^^^^^^^^^^^^^^^^^

To enter the ``mediawiki`` container, use:

.. code-block:: bash
   
   juju ssh --container git-sync <application>/<unit-ID>

From here, you can check all of the running processes:

.. code-block:: bash

   ps auxww

To check the state of the Pebble services:

.. code-block:: bash

   /charm/bin/pebble services

.. dropdown:: Expand to view a sample ``pebble services`` output from the ``mediawiki`` container

   .. terminal::
      :user: root
      :host: mediawiki-k8s-0

      /charm/bin/pebble services

      Service                Startup   Current  Since
      apache-exporter        enabled   active   today at 13:50 UTC
      clamd                  enabled   active   today at 13:50 UTC
      freshclam              enabled   active   today at 13:50 UTC
      logrotate              enabled   active   today at 13:50 UTC
      mediawiki              disabled  active   today at 14:03 UTC
      mediawikiLogs          enabled   active   today at 13:50 UTC
      redisJobChronService   disabled  active   today at 13:50 UTC
      redisJobRunnerService  disabled  active   today at 13:50 UTC

To view all of the active Pebble service logs:

.. code-block:: bash

   /charm/bin/pebble logs --follow -n=all

To view the logs of a specific Pebble service:

.. code-block:: bash

   /charm/bin/pebble logs <service> --follow -n=all

Check MediaWiki configuration files
-----------------------------------

It may be helpful to check the rendered MediaWiki configuration files.

Most configuration settings, both user- and charm-generated, can be found in ``/etc/mediawiki`` of the :ref:`MediaWiki container <how-to-troubleshoot_accessing_containers_charm_container>`:

.. terminal::
   :user: root
   :host: mediawiki-k8s-0

   ls /etc/mediawiki
   
   JobRunnerConfig.json  LateSettings.php	UserSettings.php

Running MediaWiki maintenance scripts
-------------------------------------

.. warning::

   Invoking MediaWiki maintenance scripts may have irreversible and unintended consequence. Take care to understand any potential consequences before calling a script.

   .. vale Canonical.005-Industry-product-names = NO

   You may also wish to back up the database before performing any operations. Additional information can be found in the :doc:`MySQL charm documentation <mysql:how-to/back-up-and-restore/create-a-backup>`.

   .. vale Canonical.005-Industry-product-names = YES

MediaWiki includes a number of `maintenance scripts <https://www.mediawiki.org/wiki/Manual:Maintenance_scripts>`__ used to perform various tasks. Some of these can be called using :doc:`Juju actions </reference/actions>` provided by the charm, but you may wish to call them directly.

To invoke a maintenance script, after :ref:`accessing the MediaWiki container <how-to-troubleshoot_accessing_containers_charm_container>`, use:

.. code-block:: bash

   php /var/www/html/w/maintenance/run.php <script>
