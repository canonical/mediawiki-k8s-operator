# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

run "setup_tests" {
  module {
    source = "./tests/setup"
  }
}

run "basic_deploy" {
  variables {
    model_uuid = run.setup_tests.model_uuid
    channel    = "1.45/edge"
    # renovate: depName="mediawiki-k8s"
    revision = 10
  }

  assert {
    condition     = output.application.name == "mediawiki-k8s"
    error_message = "mediawiki-k8s app_name did not match expected"
  }
}

run "integration_test" {
  variables {
    model_uuid = run.setup_tests.model_uuid
  }

  module {
    source = "./tests/integration_test"
  }

  assert {
    condition     = data.external.app_status.result.status == "blocked"
    error_message = "mediawiki-k8s app_status did not match expected"
  }
}
