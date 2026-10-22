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

set-maintenance-mode
^^^^^^^^^^^^^^^^^^^^

.. vale Canonical.007-Headings-sentence-case = YES

Maintenance work can require wiki data to remain unchanged without taking the site offline for readers. The ``set-maintenance-mode`` :ref:`action <reference_actions>` provides an operator-controlled mechanism to set the site to a read-only state, minimizing changes to durable states.

The application-level `$wgReadOnly <https://www.mediawiki.org/wiki/Manual:$wgReadOnly>`_, while useful, does not guarantee that no database writes occur. The design therefore combines database-level write protection with application-level read-only settings. The database protects against anything that does not respect ``$wgReadOnly``; this MediaWiki setting communicates that editing is unavailable and presents the maintenance reason to users. Background job workers are paused to prevent them attempting work against a read-only database.

The leader sets and verifies database protection *before* publishing the requested mode in the ``mediawiki-replica`` peer relation. This ordering makes successful action completion a confirmation of database protection without requiring every unit to have finished reconciliation. Publishing intent first could leave the application requesting maintenance mode even though database protection had failed. The same ordering applies when leaving maintenance mode: database writes are restored before units are asked to resume normal operation.

.. vale off

.. mermaid::
   :name: set-maintenance-mode-diagram

   sequenceDiagram
   actor User
   participant Leader@{ "type" : "entity" }
   participant DB as Database
   participant PR as mediawiki-replica
   participant Replicas@{ "type" : "entity" }

   User->>Leader: set-maintenance-mode enabled=true
   Leader->>DB: Set database to read-only
   DB-->>Leader: Verify read-only state
   Leader->>PR: app operation_mode = "maintenance"<br/>app maintenance_message = message
   Leader-->>User: action complete

   Note over Leader,Replicas: Units reconcile the requested mode

   Leader->>Leader: Set MediaWiki to read-only<br/>Pause job workers
   Leader->>PR: unit operation_mode = "maintenance"
   Replicas->>Replicas: Set MediaWiki to read-only<br/>Pause job workers
   Replicas->>PR: unit operation_mode = "maintenance"

   Note over Leader,Replicas: Maintenance mode persists until explicitly disabled

   User->>Leader: set-maintenance-mode enabled=false
   Leader->>DB: Set database to read-write
   DB-->>Leader: Verify read-write state
   Leader->>PR: app operation_mode = "normal"<br/>Remove maintenance_message
   Leader-->>User: action complete

   Note over Leader,Replicas: Units reconcile the requested mode

   Leader->>Leader: Set MediaWiki to read-write<br/>Resume job workers
   Leader->>PR: unit operation_mode = "normal"
   Replicas->>Replicas: Set MediaWiki to read-write<br/>Resume job workers
   Replicas->>PR: unit operation_mode = "normal"

.. vale on

The leader owns the application-wide request so that units do not make independent decisions about whether maintenance is still in progress. Peer application data carries that request across reconciliation cycles, while peer unit data records what each unit has actually applied. Separating requested and applied state allows asynchronous convergence without treating action completion as proof that every unit is ready. Unlike the database-update flow, unit acknowledgements do not trigger a return to ``normal``: only an explicit operator request ends maintenance.

Maintenance and database updates are mutually exclusive because they require different database guarantees: maintenance rejects writes, whereas a schema update must write to the database. A single operation mode also prevents competing workflows from independently restoring normal operation while another operation still needs protection. Maintenance transitions are rejected during application-wide forced reconciliation, and entry from normal operation waits for all units to have acknowledged ``normal``, avoiding overlap with an unfinished transition.

Recovery deliberately has fewer prerequisites than entry. Disabling maintenance requires a ready database relation, but not a working MediaWiki workload, so that a workload failure cannot prevent the operator from restoring database writes. Database verification failures leave the peer request unchanged and fail the action explicitly; they cannot imply that the database change was rolled back.

The protection boundary is the database, not every storage service used by MediaWiki. Valkey and Redis remain available, so maintenance mode is not a guarantee of application-wide storage inactivity. MediaWiki remains responsible for blocking writes such as job queue pushes that have durability requirements in Valkey or Redis.

.. vale Canonical.007-Headings-sentence-case = NO

update-database
^^^^^^^^^^^^^^^

.. vale Canonical.007-Headings-sentence-case = YES

Occasionally, the `database schema may need to be updated <https://www.mediawiki.org/wiki/Manual:Update.php>`_. In particular, when upgrading between major versions of MediaWiki, or when installing or updating extensions that make changes to the database schema. This is a potentially dangerous operation, so it is not something that the charm will do automatically other than the initial installation. Instead, the user has to run the ``update-database`` :ref:`action <reference_actions>`.

It is possible to do a database schema update `while the wiki is still online <https://www.mediawiki.org/wiki/Manual:Upgrading#Can_my_wiki_stay_online_while_it_is_upgrading?>`_, but it is recommended to ensure that the database is read-only during this operation. To achieve this, ``LocalSettings.php`` needs to be updated for all units. Given that :doc:`Juju actions <juju:reference/action>` are run on a single unit and in a synchronous manner, signaling using :ref:`peer relations <juju:peer-relation>` is required.

.. vale off

.. mermaid::
   :name: update-database-diagram

   sequenceDiagram
   actor User 
   participant Leader@{ "type" : "entity" }
   participant PR as mediawiki-replica
   participant Replicas@{ "type" : "entity" }

   User->>Leader: update-database action
   Leader->>PR: app operation_mode = "database-update"
   Leader-->>User: action complete

   Note over Leader,Replicas: relation_changed fires on all units

   Leader->>+Leader: Reconciliation<br/>Set wiki to read only
   Leader->>PR: unit operation_mode = "database-update"

   Replicas->>+Replicas: Reconciliation<br/>Set wiki to read only
   Replicas->>PR: unit operation_mode = "database-update"

   Note over Leader,Replicas: relation_changed fires on leader again

   break Some units have not set unit operation_mode = "database-update"
      Leader-->>Leader: WaitingStatus
   end

   Note over Leader: All units confirmed operation_mode = "database-update"
   Leader->>Leader: Update database schema
   Leader->>PR: app operation_mode = "normal"

   Note over Leader,Replicas: relation_changed fires on all units again

   Leader->>-Leader: Reconciliation<br/>Set wiki to read-write
   Leader->>PR: unit operation_mode = "normal"

   Replicas->>-Replicas: Reconciliation<br/>Set wiki to read-write
   Replicas->>PR: unit operation_mode = "normal"

.. vale on

Instead of directly running the database update maintenance script, the ``update-database`` action sets the workload mode in the peer relation application data to ``database-update``. All units, including the leader, react to this event by configuring themselves to enter a read-only mode before publishing the applied workload mode in their peer relation unit data.

The leader only proceeds with the database update once every unit reports the requested workload mode. Following the database update, it sets the application workload mode to ``normal``. Each unit then exits read-only mode and reports ``normal`` in its unit data.

Reconciliation
--------------

Composer and local settings
^^^^^^^^^^^^^^^^^^^^^^^^^^^

MediaWiki extensions and skins can be installed with `Composer <https://getcomposer.org/>`_, a PHP dependency manager. The charm manages this through a ``composer.user.json`` file, which is written into the MediaWiki installation directory and merged with the base MediaWiki composer configurations. When libraries are configured by the operator, Composer resolves and installs them.

In a multi-unit deployment, units should converge to identical Composer packages. If each unit independently resolved dependencies, different units could end up on different package versions, potentially leading to inconsistent behavior. Normally, only the leader resolves dependencies (``composer update``) while all other units install exactly what the leader resolved (``composer install`` against the leader-published lock file). Empty replacement workloads have a narrowly scoped recovery exception, described below.

The charm also synchronizes the user-provided ``LocalSettings.php`` content across units. The leader publishes a state hash alongside the Composer lock file. This hash combines the configured Composer dependencies and local settings, allowing non-leader units to verify that their configuration matches the state used to produce the published lock before they reconcile. Unless eligible for recovery, they enter ``WaitingStatus`` if it does not match, until the leader publishes state for the new configuration.

.. vale off

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
   PR->>PrePublishReplica: composer_lock + state_hash (absent)
   alt Eligible empty replacement workload
      PrePublishReplica->>PrePublishReplica: composer update using local config<br/>Write settings and start web services
      Note over PR,PrePublishReplica: App lock and hash remain leader-owned
   else Existing coordination applies
      PrePublishReplica-->>PrePublishReplica: WaitingStatus<br/>(retries on the next event)
   end
   end

   Config->>Leader: composer config
   opt composer.user.json differs from config, or update forced
      Leader->>Leader: write composer.user.json
      Leader->>Leader: composer update<br/>(resolves latest compatible versions)
   end
   Leader->>Leader: read composer.lock<br/>from container
   Leader->>PR: composer_lock + state_hash<br/>(Composer + local settings)

   PR->>Replicas: composer_lock + state_hash
   alt local state differs from published state
      alt Eligible empty replacement workload
         Replicas->>Replicas: composer update using local config<br/>Write settings and start web services
      else Existing coordination applies
         Replicas-->>Replicas: WaitingStatus
      end
   else Composer files differ from peer data
      Replicas->>Replicas: write composer.user.json + composer.lock<br/>(from peer data)
      Replicas->>Replicas: composer install<br/>(installs exact versions from lock)
   end

.. vale on

This process is designed according to the following principles:

- **Leader as single source of truth**: Normally, only the leader unit runs ``composer update``, which resolves and pins dependency versions. Its resulting ``composer.lock`` remains the authoritative dependency manifest, including after temporary independent recovery.
- **The leader always publishes the lock and state hash**: At the end of every reconciliation the leader reads ``composer.lock`` and publishes it with a hash of the configured Composer dependencies and local settings, even when ``composer update`` was skipped. This ensures that non-leaders can verify that the lock belongs to their current configuration.
- **The lock matches the config**: Outside the recovery exception, non-leader units only proceed when their Composer and local-settings configuration matches the state hash published by the leader. Recovery resolves dependencies locally rather than combining new settings with an incompatible old leader lock.
- **Non-leaders install from the lock**: Outside the recovery exception, non-leader units write the leader-published lock file to disk *before* invoking Composer. This means that Composer installs exactly the packages and versions already resolved by the leader, regardless of what the current charm config resolves to independently.
- **Idempotent skipping without halting reconciliation**: Before running Composer, each unit checks whether its on-disk state already matches the desired state. Leaders skip when ``composer.user.json`` matches the current config, while non-leaders skip when both ``composer.user.json`` and ``composer.lock`` match what the leader published. Skipping only short-circuits the Composer step, so the rest of the reconciliation still runs. A stale or missing lock on a non-leader will always trigger a re-install.
- **Configured or running non-leaders wait if state is unavailable or stale**: If the leader has not yet published a lock and matching state hash, or if a non-leader's configuration differs from the published state, it aborts reconciliation and enters ``WaitingStatus`` rather than attempting to resolve dependencies itself. Running web services remain available while the unit waits at this gate.

Recovery of empty replacement workloads
"""""""""""""""""""""""""""""""""""""""

During a refresh, workload containers can be replaced before the leader reconciles a concurrent configuration change. If all non-leaders wait without starting their replacement workloads, refreshing the leader last can leave no unit serving the wiki.

To reduce this availability gap, a non-leader may reconcile independently when the leader's state hash is missing or stale, but only if all these conditions hold:

- The application operation mode is ``normal``.
- Neither ``LocalSettings.php`` nor ``composer.user.json`` exists in the workload container.
- Neither the MediaWiki nor PHP-FPM service is running.
- The shared database is already initialized.

The unit still requires its peer relation, shared secrets, and ready workload relations. Recovery uses the leader-style Composer update path with the current local configuration, generates settings, rebuilds the localisation cache, and starts web services. Composer retains its normal skip behavior when the user configuration is empty. Recovery does not publish an application lock or hash, change the requested operation mode, or upgrade the database schema.

Dependency versions may temporarily differ from those on other units. Once the leader publishes a matching hash and lock, the recovered unit installs that authoritative lock through normal reconciliation. If synchronized leader inputs are already available, even an empty workload uses the normal install path instead.

This exception does not apply to first installation, configured but stopped workloads, running workloads, or maintenance, database-update, and forced-reconciliation modes. Once a unit has written either its user Composer configuration or settings, it no longer qualifies for independent recovery. The Composer configuration is written before the command runs, so even a failed Composer attempt prevents another independent attempt. Subsequent reconciliation waits for synchronized leader inputs before using the normal install path. Recovery errors surface through the existing blocked or waiting statuses. This behavior reduces one refresh-related availability gap, but does not guarantee uninterrupted service across incompatible image, extension, or database-schema changes.

Localisation cache
^^^^^^^^^^^^^^^^^^

Each unit stores its `localisation cache <https://www.mediawiki.org/wiki/Manual:LocalisationCache.php>`_ on its own filesystem rather than in MediaWiki's database. This avoids cache-related database traffic and speeds up access. The charm also sets `$wgLocalisationCacheConf['manualRecache'] <https://www.mediawiki.org/wiki/Manual:$wgLocalisationCacheConf>`_ to ``true`` so MediaWiki does not rebuild the cache during web requests. Instead, the charm runs the `rebuildLocalisationCache <https://www.mediawiki.org/wiki/Manual:RebuildLocalisationCache.php>`_ maintenance script during reconciliation when the cache might be stale.

Specifically, each unit rebuilds the cache during reconciliation for the following reasons:

- Its peer data has no record of a successful rebuild, such as on first installation.
- The MediaWiki version differs from the version recorded at the last successful rebuild, since core translations may have changed.
- Its settings files change, which may affect localisation.
- Composer runs, potentially changing translations supplied by extensions or skins.
- Reconciliation is forced.

The forced reconciliation case is special, and the charm will pass ``--force`` to ``rebuildLocalisationCache`` to rebuild all entries regardless of necessity.

The charm keeps a pending rebuild marker alongside the cache until the rebuild succeeds. This ensures that changes are not lost if reconciliation stops after updating settings or running Composer but before rebuilding the cache. On success, the unit records the MediaWiki version in its peer data and clears the marker. On failure, the unit enters a blocked status and retries the pending rebuild on the next reconciliation.
