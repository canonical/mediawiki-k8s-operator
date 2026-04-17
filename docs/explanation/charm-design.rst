.. meta::
   :description: Understand the architectural design decisions and code structure of the MediaWiki charm.

.. _explanation_charm_design:

Charm design
============

This page provides detailed explanations on particular design decisions made in the development of the MediaWiki charm.

   See the :ref:`charm architecture reference page <reference_charm_architecture>` for a general overview of the charm architecture.

Actions
--------

.. vale Canonical.007-Headings-sentence-case = NO

update-database
^^^^^^^^^^^^^^^

.. vale Canonical.007-Headings-sentence-case = YES

Occasionally, the `database schema may need to be updated <https://www.mediawiki.org/wiki/Manual:Update.php>`_. In particular, when upgrading between major versions of MediaWiki, or when installing or updating extensions that make changes to the database schema. This is a potentially dangerous operation, so it is not something that the charm will do automatically other than the initial installation. Instead, the user has to run the ``update-database`` :ref:`action <reference_actions>`.

It is possible to do a database schema update `while the wiki is still online <https://www.mediawiki.org/wiki/Manual:Upgrading#Can_my_wiki_stay_online_while_it_is_upgrading?>`_, but it is recommended to ensure that the database is read-only during this operation. To achieve this, ``LocalSettings.php`` needs to be updated for all units. Given that :doc:`Juju actions <juju:reference/action>` are run on a single unit and in a synchronous manner, signaling using :ref:`peer relations <juju:peer-relation>` is required.

.. mermaid::
   :name: update-database-diagram

   sequenceDiagram
   actor User 
   participant Leader@{ "type" : "entity" }
   participant PR as mediawiki-replica
   participant Replicas@{ "type" : "entity" }

   User->>Leader: update-database action
   Leader->>PR: app ro_db = "true"
   Leader-->>User: action complete

   Note over Leader,Replicas: relation_changed fires on all units

   Leader->>+Leader: Reconciliation<br/>Set wiki to read only
   Leader->>PR: unit ro_db = "true"

   Replicas->>+Replicas: Reconciliation<br/>Set wiki to read only
   Replicas->>PR: unit ro_db = "true"

   Note over Leader,Replicas: relation_changed fires on leader again

   break Some units have not set unit ro_db = "true"
      Leader-->>Leader: WaitingStatus
   end

   Note over Leader: All units confirmed ro_db = "true"
   Leader->>Leader: Update database schema
   Leader->>PR: app ro_db = "false"

   Note over Leader,Replicas: relation_changed fires on all units again

   Leader->>-Leader: Reconciliation<br/>Set wiki to read-write
   Leader->>PR: unit ro_db = "false"

   Replicas->>-Replicas: Reconciliation<br/>Set wiki to read-write
   Replicas->>PR: unit ro_db = "false"

Instead of directly running the database update maintenance script, the ``update-database`` action will simply set a flag in the peer relation application data to indicate that the database should be updated. All units, including the leader, will react to this by configuring themselves to enter a read-only mode and then set a flag in the peer relation unit data to indicate that it has done so.

The leader will only proceed with the database update once it determines that all units have set the flag to indicate that they are in read-only mode. Following the database update, it will unset the original application flag to indicate to all units that they can exit read-only mode. As they do so, they will unset their unit flags.
