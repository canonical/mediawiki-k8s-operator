.. meta::
   :description: How to serve static assets from a Git repository in the MediaWiki charm.

.. _how_to_use_static_assets:

How to use static assets
========================

.. vale Canonical.000-US-spellcheck = NO

The MediaWiki charm can sync static assets, such as custom logos, from a Git repository and serve them through the web server. The assets are cloned into the MediaWiki container by the :ref:`git-sync sidecar <reference_charm_architecture_containers_git_sync>` and served under the ``/static`` path.

.. vale Canonical.000-US-spellcheck = YES

Configure the repository
------------------------

Set the ``static-assets-git-repo`` configuration option to the URL of the repository to clone. HTTP(S), SSH, and SCP-style (``git@github.com:user/repo.git``) URLs are supported.

.. code-block:: bash

   juju config mediawiki-k8s static-assets-git-repo="https://github.com/my-org/my-assets.git"

The repository contents are cloned into ``$charmStaticAssets`` inside the MediaWiki container, which resolves to the ``/static`` web path. For example, a file named ``my_logo.svg`` in the root of the repository is served at ``/static/my_logo.svg``.

.. note::

   Hidden files (those starting with a dot) are not served by the web server.

To pin the assets to a specific branch, tag, or commit, set ``static-assets-git-ref``. If left unset, the ``HEAD`` of the repository's default branch is used.

.. code-block:: bash

   juju config mediawiki-k8s static-assets-git-ref="v1.0.0"

Limit which files are checked out
---------------------------------

To check out only a subset of the repository, such as a single directory or specific files, set ``static-assets-git-sparse-checkout`` to the contents of a `sparse-checkout file <https://git-scm.com/docs/git-sparse-checkout>`_:

.. code-block:: bash

   juju config mediawiki-k8s static-assets-git-sparse-checkout="$(cat ${PATH_TO_SPARSE_CHECKOUT_FILE})"

Only paths matching the patterns in the file are present under ``$charmStaticAssets``. This is useful for excluding files that should not be served, such as a ``README`` or ``LICENSE``.

Sync from a private repository
------------------------------

To clone from a private repository over SSH, provide an SSH private key through a :ref:`Juju user secret <juju:user-secret>` referenced by the ``ssh-key`` configuration option. The key must be placed in the secret's ``git-sync`` field:

.. vale Canonical.016-No-inline-comments = NO

.. code-block:: bash

   juju add-secret mediawiki-ssh-keys git-sync#file="${PATH_TO_PRIVATE_KEY}"
   juju grant-secret mediawiki-ssh-keys mediawiki-k8s
   juju config mediawiki-k8s ssh-key=<secret-id>

.. vale Canonical.016-No-inline-comments = YES

If the repository is hosted somewhere other than ``github.com`` or ``git.launchpad.net``, add the host's public key to the ``ssh-known-hosts`` configuration option, otherwise the charm will block.

.. code-block:: bash

   juju config mediawiki-k8s ssh-known-hosts="$(ssh-keyscan gitlab.com)"

Use the assets in MediaWiki
---------------------------

Synced assets are not referenced by MediaWiki automatically. Point MediaWiki at them through the ``local-settings`` :doc:`configuration option </how-to/configure-mediawiki>`. For example, to set the wiki logo:

.. code-block:: php

   $wgLogos = [ '1x' => "$charmStaticAssets/my_logo.svg" ];

Clear synced assets
-------------------

If a clone fails, any previously synced assets are kept in place. To remove the assets, reset ``static-assets-git-repo`` to its default value:

.. code-block:: bash

   juju config --reset mediawiki-k8s static-assets-git-repo

.. seealso::

   For security considerations when serving static assets, refer to the :doc:`security overview </explanation/security>`.
