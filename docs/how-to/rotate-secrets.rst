.. meta::
   :description: How to rotate secrets in the MediaWiki charm.

.. _how_to_rotate_secrets:

How to rotate secrets
=====================

To rotate secrets managed by the MediaWiki charm, run the ``rotate-mediawiki-secrets`` Juju action on the leader unit:

.. code-block:: bash

   juju run mediawiki-k8s/leader rotate-mediawiki-secrets

After rotating secrets, session information may be lost. 

.. seealso::

    For more information on the secrets managed by the MediaWiki charm, refer to the :ref:`security overview <explanation_security_secrets>` documentation page.
