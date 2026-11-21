import {
  for_each = local.apis

  to = google_project_service.api[each.key]
  id = "${local.project_id}/${each.key}"
}

import {
  to = google_artifact_registry_repository.app
  id = "projects/${local.project_id}/locations/${local.region}/repositories/lucromei"
}

import {
  to = google_service_account.runtime
  id = "projects/${local.project_id}/serviceAccounts/lucromei-runtime@${local.project_id}.iam.gserviceaccount.com"
}

# Secrets that existed before Terraform adopted the project. Secrets added later are created
# by Terraform and must not be imported.
locals {
  adopted_secret_ids = setsubtract(local.secret_ids, [
    "lucromei-privacy-hash-key",
    "lucromei-sentry-dsn",
    "lucromei-stripe-test-secret-key",
    "lucromei-stripe-test-webhook-secret",
  ])
  # The migration secret was created by hand, but its runtime grant is created by Terraform.
  adopted_secret_grants = setsubtract(local.adopted_secret_ids, [local.migrate_database_secret])
}

import {
  for_each = local.adopted_secret_ids

  to = google_secret_manager_secret.app[each.key]
  id = "projects/${local.project_id}/secrets/${each.key}"
}

import {
  for_each = local.adopted_secret_grants

  to = google_secret_manager_secret_iam_member.runtime_accessor[each.key]
  id = "projects/${local.project_id}/secrets/${each.key} roles/secretmanager.secretAccessor serviceAccount:lucromei-runtime@${local.project_id}.iam.gserviceaccount.com"
}

import {
  to = google_cloud_run_v2_service.test
  id = "projects/${local.project_id}/locations/${local.region}/services/${local.service_name}"
}

# Roles granted before Terraform adopted them. Roles added later (monitoring) are created here
# by an IAM admin's apply, so they must not be imported.
import {
  for_each = setsubtract(local.operator_roles, ["roles/monitoring.editor", "roles/logging.configWriter"])

  to = google_project_iam_member.operator[each.key]
  id = "${local.project_id} ${each.key} ${local.operator}"
}

import {
  to = google_cloudbuildv2_connection.github
  id = "projects/${local.project_id}/locations/${local.region}/connections/lucromei-github"
}
