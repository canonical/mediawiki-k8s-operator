.. meta::
   :description: Discover the MediaWiki charm, a Juju operator that deploys and manages MediaWiki.

.. vale Canonical.007-Headings-sentence-case = NO

.. _index:

MediaWiki operator
========================

.. vale Canonical.007-Headings-sentence-case = YES

A `Juju <https://juju.is/>`_ `charm <https://documentation.ubuntu.com/juju/3.6/reference/charm/>`_
deploying and managing `MediaWiki`_ on Kubernetes.
MediaWiki is a free and open-source wiki software developed by the Wikimedia Foundation, the host of Wikipedia.

Like any Juju charm, this charm supports one-line deployment, configuration, integration,
scaling, and more. 
For MediaWiki, this includes:

* Direct access to MediaWiki's basic configuration settings
* S3 backed object storage for redundant storage of file uploads
* Integration with SSO

The MediaWiki charm allows for deployment on many different Kubernetes platforms,
from `MicroK8s <https://microk8s.io/>`_ to 
`Charmed Kubernetes <https://ubuntu.com/kubernetes>`_ to public cloud Kubernetes offerings.

This charm will make operating MediaWiki simple and straightforward for DevOps or
SRE teams through Juju's clean interface. 

In this documentation
---------------------

Get started
^^^^^^^^^^^

Learn about what's in the charm, step through a basic deployment, and perform some common operations.

- **Learn, try, and plan**: :ref:`Guided tutorial <tutorial_index>` • :ref:`High-level deployment <reference_high_level_deployment>` 
- **Deploy and configure**: :ref:`Relation endpoints <reference_relation_endpoints>`
- **Observe, maintain, and update**: :ref:`Metrics <reference_metrics>`

Dive deeper
^^^^^^^^^^^

Learn more about operations focused on advanced configurations and security.

.. - **Advanced operations**: Relevant how-to guides • Relevant reference pages 
.. - **Charm-specific topic**: Relevant how-to guides • Relevant reference pages
.. - **Security**: Overview • Relevant how-to guides • Relevant reference pages
.. - **Design**: Architecture • Design

- **Troubleshooting**: :ref:`How to troubleshoot <how_to_troubleshoot>`

Develop and contribute
^^^^^^^^^^^^^^^^^^^^^^^

.. - **Development**: Terraform-related docs (if applicable) • Developer-related docs (if applicable)

- **Learn more about the charm**: :ref:`Design <explanation_charm_design>` • :ref:`Releases <release_notes_index>` • :ref:`changelog`
- Get involved: :ref:`Contribute to the documentation <how_to_contribute>` • `Contribute to the source code <CONTRIBUTING.md_>`_

How this documentation is organized
------------------------------------

This documentation uses the `Diátaxis documentation structure <https://diataxis.fr/>`_.

- The :ref:`Tutorial <tutorial_index>` takes you step-by-step through a basic deployment of the `MediaWiki`_ charm.
- :ref:`How-to guides <how_to_index>` assume you have basic familiarity with the `MediaWiki`_ charm. Learn more about setting up, using, maintaining, and contributing to this charm.
- :ref:`Reference <reference_index>` provides a guide to actions, configurations, relations, and other technical details.
- :ref:`Explanation <explanation_index>` includes topic overviews, background and context and detailed discussion.
- :ref:`Release notes <release_notes_index>` holds all the release notes for the charm, including any system or upgrade requirements.

Contributing to this documentation
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Documentation is an important part of this project, and we take the same open-source approach
to the documentation as the code. As such, we welcome community contributions, suggestions, and
constructive feedback on our documentation.
See :ref:`How to contribute <how_to_contribute>` for more information.

If there's a particular area of documentation that you'd like to see that's missing, please 
`file a bug`_.

Project and community
---------------------

The MediaWiki Operator is a member of the Ubuntu family. It's an open-source project that warmly welcomes community 
projects, contributions, suggestions, fixes, and constructive feedback.

Governance and policies
^^^^^^^^^^^^^^^^^^^^^^^

- `Code of conduct <https://ubuntu.com/community/code-of-conduct>`_

Get involved
^^^^^^^^^^^^

- `Get support <https://discourse.charmhub.io/>`_
- `Join our online chat <https://matrix.to/#/#charmhub-charmdev:ubuntu.com>`_
- :ref:`Contribute <how_to_contribute>`

Releases
^^^^^^^^

- :ref:`Release notes <release_notes_index>`

Thinking about using the MediaWiki Operator for your next project? 
`Get in touch <https://matrix.to/#/#charmhub-charmdev:ubuntu.com>`_!

.. vale Canonical.013-Spell-out-numbers-below-10 = NO
.. vale Canonical.500-Repeated-words = NO

.. toctree::
    :hidden:
    :maxdepth: 1

    Tutorial <tutorial/index>
    How-to guides <how-to/index>
    Reference <reference/index>
    Explanation <explanation/index>
    Release notes <release-notes/index>

