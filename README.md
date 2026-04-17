# MediaWiki K8s operator

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

- `rotate-root-credentials`
- `update-database`

<!-- Uncomment when pages are ready
You can find more information about supported actions in [the Charmhub documentation](). <!--Link to the charm's actions documentation>

The charm supports further customization, including:

- [Installing additional extensions and skins]()
- [Configuring MediaWiki]()
- [Integrating with S3 object storage for file uploads]()
 -->

For more information, refer to the [charm's documentation][charm-documentation-site]. <!--Link to primary RTD documentation-->

## Integrations

Deployment of MediaWiki requires a relational database. For the MediaWiki charm, this means that integration with the MQL interface is a mandatory minimal requirement.

With a `mediawiki-k8s` deployment, this can be done with the following:

```bash
juju deploy mysql-k8s --trust
juju integrate mediawiki-k8s mysql-k8s:database
```

Apart from this required integration, the charm can be integrated with other Juju charms and services as well. You can find the full list of integrations in [the Charmhub documentation][charm-documentation-site]. <!--Link to the charm's integration documentation-->

## Learn more

- [Read more][charm-documentation-site] <!--Link to the charm's official documentation-->
- [Developer documentation](https://www.mediawiki.org/wiki/Developer_hub)
- [Official webpage][mediawiki-site]

## Project and community

- [Issues](https://github.com/canonical/mediawiki-k8s-operator/issues)
- [Contributing](CONTRIBUTING.md)

## Licensing and trademark

<!--vale Canonical.013-Spell-out-numbers-below-10 = NO-->

| Component                                          | License                                                                                                    | Notes                                                                                           |
| -------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| MediaWiki K8s operator                             | [Apache License, Version 2.0][apache-2.0-site]                                                             |                                                                                                 |
| [MediaWiki K8s operator documentation](docs)       | [CC BY-SA 4.0][cc-by-sa-4.0-site]                                                                          |                                                                                                 |
| [MediaWiki][mediawiki-site]                        | [GNU General Public License, version 2.0 or later](https://www.gnu.org/licenses/old-licenses/gpl-2.0.html) | See [MediaWiki copyright information](https://www.mediawiki.org/wiki/Copyright)                 |
| [MediaWiki rock](mediawiki_rock)                   | [GNU General Public License, version 3](https://www.gnu.org/licenses/gpl-3.0.html)                         | Packages and redistributes [MediaWiki][mediawiki-site]                                          |
| [git-sync](https://github.com/kubernetes/git-sync) | [Apache License, Version 2.0][apache-2.0-site]                                                             | See the upstream [git-sync LICENSE](https://github.com/kubernetes/git-sync/blob/master/LICENSE) |
| [MediaWiki logo](icon.svg)                         | [CC BY-SA 4.0][cc-by-sa-4.0-site]                                                                          |                                                                                                 |

<!--vale Canonical.013-Spell-out-numbers-below-10 = YES-->

MediaWiki and the MediaWiki logo are trademarks of the Wikimedia Foundation and are used with permission of the Wikimedia Foundation. This project is not endorsed by, or affiliated with, the Wikimedia Foundation.

For additional details, please refer to the [LICENSE](LICENSE).

<!--Placeholder, update this when available-->

[apache-2.0-site]: https://www.apache.org/licenses/LICENSE-2.0
[cc-by-sa-4.0-site]: https://creativecommons.org/licenses/by-sa/4.0/
[charm-documentation-site]: https://documentation.ubuntu.com
[mediawiki-site]: https://www.mediawiki.org
