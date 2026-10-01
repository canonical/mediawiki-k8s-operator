.. meta::
   :description: Reference documentation for the log levels and log forwarding behavior of the MediaWiki charm.

.. _reference_logging:

Logging
=======

The charm does not use one global log level for all components. MediaWiki, Apache, and other workload services have separate logging behavior. When the :ref:`logging relation <reference_relation_endpoints_logging>` is established, the charm forwards workload standard output and standard error to the configured log consumer, such as Loki.

.. warning::
   Logs produced by the MediaWiki charm's workloads may contain sensitive information such as secrets, values of submitted forms, and IP addresses. Handle and share these logs with care.
   
   Log files such as ``/var/log/mediawiki/logs.log`` and the Apache logs are kept on disk in the workload container until they are automatically rotated.

MediaWiki application logs
--------------------------

MediaWiki logging is based on named diagnostic groups rather than a single severity threshold. MediaWiki's ``$wgDebugLogGroups`` setting routes messages from these groups to log destinations; see the `MediaWiki documentation for $wgDebugLogGroups <https://www.mediawiki.org/wiki/Manual:$wgDebugLogGroups>`__ for details. By default, the charm routes the following groups to ``/var/log/mediawiki/logs.log``:

* ``exception``
* ``error``
* ``fatal``
* ``HttpError``
* ``ratelimit``
* ``throttler``

General MediaWiki debug logging through ``$wgDebugLogFile`` is disabled by default. It can be enabled independently of the selected groups, but can produce substantial output. Disabling general debugging does not disable selected-group logging.

Both ``$wgDebugLogGroups`` and ``$wgDebugLogFile`` are :ref:`charm-managed settings <reference_charm_managed_settings>`, controlled by the ``mediawiki-debug-log-groups`` and ``mediawiki-debug-log`` :ref:`configuration options <reference_configurations>`, respectively. The charm overrides assignments to these settings in user-supplied ``LocalSettings.php`` content.

A Pebble-managed service tails the managed log file and writes new entries to the MediaWiki container's standard output. This makes the entries available through the workload logs and the logging relation.

Apache logs
-----------

The MediaWiki rock sets Apache's ``LogLevel`` to ``warn``; this is not configurable through the charm. Apache error and access logs are also written to the Apache Pebble service's standard error and standard output, respectively, so they are included in forwarded workload logs.

Pebble-managed services
-----------------------

The charm runs workload processes as Pebble-managed services. The logging relation forwards output captured from workload containers' standard output and standard error, including console output from Pebble-managed services. Arbitrary log files written elsewhere are not collected unless explicitly streamed by a dedicated Pebble-managed service such as ``mediawikiLogs``.

Charm logs
----------

Charm and Juju agent logs are separate from workload logs and are not forwarded through the logging relation. Their verbosity is controlled by Juju's model ``logging-config`` setting, not by the workload logging defaults described on this page.

.. seealso::
   See the :doc:`how to troubleshoot guide <../how-to/troubleshoot>` for an example of changing the unit log level.

   For general information on managing the charm logs, refer to the :doc:`Juju documentation on managing logs <juju:howto/manage-logs>`.
