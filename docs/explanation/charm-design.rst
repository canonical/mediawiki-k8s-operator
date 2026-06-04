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

Instead of directly running the database update maintenance script, the ``update-database`` action sets a flag in the peer relation application data to indicate that the database should be updated. All units, including the leader, will react to this event by configuring themselves to enter a read-only mode before setting a flag in the peer relation unit data to indicate that it has done so.

The leader will only proceed with the database update once it determines that all units have set the flag to indicate that they are in read-only mode. Following the database update, it will unset the original application flag to indicate to all units that they can exit read-only mode. As they do so, they will unset their unit flags.

Reconciliation
--------------

Composer
^^^^^^^^

MediaWiki extensions and skins can be installed with `Composer <https://getcomposer.org/>`_, a PHP dependency manager. The charm manages this through a ``composer.user.json`` file, which is written into the MediaWiki installation directory and merged with the base MediaWiki composer configurations. When libraries are configured by the operator, Composer resolves and installs them.

In a multi-unit deployment, all units must run with identical Composer packages. If each unit independently resolved dependencies, different units could end up on different package versions, potentially leading to inconsistent behavior. To avoid this, the charm ensures that only the leader resolves dependencies (``composer update``) while all other units install exactly what the leader resolved (``composer install`` against the leader-published lock file).

.. mermaid::
   :name: composer-sync-diagram

   sequenceDiagram
   participant Config as Charm config
   participant Leader@{ "type" : "entity" }
   participant PR as mediawiki-replica
   participant Replicas@{ "type" : "entity" }
   participant PrePublishReplica@{ "type" : "entity" } as Replicas (pre-publish)

   rect rgb(245, 245, 245)
   Note over PR,PrePublishReplica: Replica reconciles before leader has published
   PR->>PrePublishReplica: composer_json + composer_lock (absent)
   break Leader has not yet published
      PrePublishReplica-->>PrePublishReplica: WaitingStatus<br/>(retries on the next event)
   end
   end

   Config->>Leader: composer config
   opt composer.user.json differs from config, or update forced
      Leader->>Leader: write composer.user.json
      Leader->>Leader: composer update<br/>(resolves latest compatible versions)
   end
   Leader->>Leader: read composer.lock<br/>from container
   Leader->>PR: composer_json = serialised json<br/>composer_lock = lock content

   PR->>Replicas: composer_json<br/>composer_lock
   opt composer.user.json + composer.lock differ from peer data
      Replicas->>Replicas: write composer.user.json + composer.lock<br/>(from peer data)
      Replicas->>Replicas: composer install<br/>(installs exact versions from lock)
   end

This process is designed according to the following principles:

- **Leader as single source of truth**: Only the leader unit runs ``composer update``, which resolves and pins dependency versions. This prevents version drift between units. The resulting ``composer.lock`` is treated as the authoritative dependency manifest.
- **The leader always publishes the lock**: At the end of every reconciliation the leader reads ``composer.lock`` and publishes it, even when ``composer update`` was skipped. This ensures that non-leaders are not perpetually blocked waiting for a lock. As the ``relation_changed`` event is only triggered when the contents of the data bag change, this behavior does not result in unnecessary reconciliation calls.
- **The lock matches the config**: Instead of pulling from the charm config, replica units use the composer config published by the leader. This prevents a race condition that could otherwise result in a mismatch between the composer config and lock file on replicas.
- **Non-leaders installs from the lock**: Non-leader units write the leader-published lock file to disk *before* invoking Composer. This means that Composer installs exactly the packages and versions already resolved by the leader, regardless of what the current charm config resolves to independently.
- **Idempotent skipping without halting reconciliation**: Before running Composer, each unit checks whether its on-disk state already matches the desired state. Leaders skip when ``composer.user.json`` matches the current config, while non-leaders skip when both ``composer.user.json`` and ``composer.lock`` match what the leader published. Skipping only short-circuits the Composer step, so the rest of the reconciliation still runs. A stale or missing lock on a non-leader will always trigger a re-install.
- **Non-leaders wait if no data is available**: If the leader has not yet published a composer pair, non-leaders abort the reconciliation process and enter ``WaitingStatus`` rather than attempting to resolve dependencies themselves.
