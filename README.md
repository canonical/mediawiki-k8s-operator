# MediaWiki K8s operator

[![Charmhub Badge][charmhub-badge-image]][charmhub-site]
[![Publish to edge][publish-charm-badge-image]][publish-charm-workflow]
[![Promote charm][promote-charm-badge-image]][promote-charm-workflow]
[![Charm documentation][charm-documentation-image]][charm-documentation-site]
[![Allure report deployment][allure-report-image]][allure-report-site]

A [Juju](https://juju.is/) [charm](https://documentation.ubuntu.com/juju/3.6/reference/charm/) deploying and managing MediaWiki on Kubernetes. [MediaWiki][mediawiki-site] is a free and open-source wiki software platform.

For information about how to deploy, integrate, and manage this charm, see the Official [MediaWiki K8s charm documentation][charm-documentation-site].

## Get started

<!--If the charm already contains a relevant how-to guide or tutorial in its documentation,
use this section to link the documentation. You don’t need to duplicate documentation here.
If the tutorial is more complex than getting started, then provide brief descriptions of the
steps needed for the simplest possible deployment. Make sure to include software and hardware
prerequisites.

This section could be structured in the following way:

### Set up
<Steps for setting up the environment (e.g. via Multipass)>

### Deploy
<Steps for deploying the charm>

-->

### Basic operations

The following actions are available for the charm:

- `rotate-mediawiki-secrets`
- `rotate-root-credentials`
- `update-database`

You can find more information about supported actions in [the Charmhub documentation](https://charmhub.io/mediawiki-k8s/actions).

The charm supports further customization, including:

- [Configuring MediaWiki](https://canonical-mediawiki-k8s-charm.readthedocs-hosted.com/how-to/configure-mediawiki/)
- [Installing additional extensions and skins](https://canonical-mediawiki-k8s-charm.readthedocs-hosted.com/how-to/install-extensions-and-skins/)

<!-- Uncomment when pages are ready
- [Integrating with S3 object storage for file uploads]()
 -->

For more information, refer to the [charm's documentation][charm-documentation-site].

## Integrations

Deployment of MediaWiki requires a relational database. For the MediaWiki charm, this means that integration with the MQL interface is a mandatory minimal requirement.

With a `mediawiki-k8s` deployment, this can be done with the following:

```bash
juju deploy mysql-k8s --trust
juju integrate mediawiki-k8s mysql-k8s:database
```

Apart from this required integration, the charm can be integrated with other Juju charms and services as well. You can find the full list of integrations in [the Charmhub documentation](https://charmhub.io/mediawiki-k8s/integrations).

## Learn more

- [Read more][charm-documentation-site]
- [Developer documentation](https://www.mediawiki.org/wiki/Developer_hub)
- [Official webpage][mediawiki-site]

## Project and community

- [Issues](https://github.com/canonical/mediawiki-k8s-operator/issues)
- [Contributing](CONTRIBUTING.md)
- [Matrix](https://matrix.to/#/#charmhub-charmdev:ubuntu.com)

## Licensing and trademark

The [MediaWiki logo](icon.svg) is licensed under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/).

MediaWiki and the MediaWiki logo are trademarks of the Wikimedia Foundation and is used with the permission of the Wikimedia Foundation. We are not endorsed by or affiliated with the Wikimedia Foundation.

[charmhub-badge-image]: https://charmhub.io/mediawiki-k8s/badge.svg
[charmhub-site]: https://charmhub.io/mediawiki-k8s
[publish-charm-badge-image]: https://github.com/canonical/mediawiki-k8s-operator/actions/workflows/publish_charm.yaml/badge.svg
[publish-charm-workflow]: https://github.com/canonical/mediawiki-k8s-operator/actions/workflows/publish_charm.yaml
[promote-charm-badge-image]: https://github.com/canonical/mediawiki-k8s-operator/actions/workflows/promote_charm.yaml/badge.svg
[promote-charm-workflow]: https://github.com/canonical/mediawiki-k8s-operator/actions/workflows/promote_charm.yaml
[charm-documentation-image]: https://img.shields.io/website?url=https%3A%2F%2Fcanonical-mediawiki-k8s-charm.readthedocs-hosted.com%2F&logo=readthedocs&label=Documentation
[charm-documentation-site]: https://canonical-mediawiki-k8s-charm.readthedocs-hosted.com/
[allure-report-image]: https://img.shields.io/github/deployments/canonical/mediawiki-k8s-operator/github-pages?label=Allure%20report
[allure-report-site]: https://canonical.github.io/mediawiki-k8s-operator/
[mediawiki-site]: https://www.mediawiki.org
