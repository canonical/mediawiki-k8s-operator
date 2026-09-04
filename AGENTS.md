# MediaWiki K8s Operator

This repository is a Juju Kubernetes charm that deploys and manages MediaWiki.

## Structure

- `src/charm.py` is the charm entry point and reconciliation boundary.
- `src/mediawiki/` manages MediaWiki configuration and lifecycle.
- `src/` modules manage relation-specific integrations and container operations.
- `lib/charms/` contains vendored Charmhub hosted charm libraries.
- `tests/unit/` exercises charm and module behavior; `tests/integration/` exercises a deployed charm.
- `mediawiki_rock/` builds the MediaWiki OCI image consumed by the charm.
- `terraform/` is the Terraform module for deploying this charm through the Juju provider.
- `docs/` contains the published charm documentation.

## Change Loop

1. Trace a requested behavior through the charm event handler, relation module, and MediaWiki layer it affects.
2. Keep the change at the narrowest layer that owns the behavior; add or update the corresponding focused test.
3. Run the narrowest local check that covers the change. Escalate to deployed testing only when its evidence is required. The change is complete when the selected checks pass and the user-visible charm behavior is covered.

## Project Rules

- Preserve the copyright and licensing header used by Python and build files.
- Treat relation data, workload configuration, and persisted peer state as compatibility boundaries.

## Pointers

- **Development setup:** read `CONTRIBUTING.md` for human environment setup, builds, and deployment.
- **Local validation:** read `tox.toml` and `pyproject.toml` for the defined checks and tool configuration. Prefer focused unit, lint, static, and documentation checks.
- **Deployed validation:** run only when explicitly requested and target a specific test. Read `spread.yaml`, `tests/integration/`, and the CI workflow first; use opcli to run the test, which provisions its scratch environment. Direct `tox -e integration` and Juju deployment can mutate the current environment, so use them only with explicit approval for that environment.
- **Documentation:** inspect `docs/` and run the applicable documentation checks defined by `Makefile.docs`.
