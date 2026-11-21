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
  adopted_secret_ids = setsubtract(local.secret_ids, ["lucromei-privacy-hash-key"])
}

import {
  for_each = local.adopted_secret_ids

  to = google_secret_manager_secret.app[each.key]
  id = "projects/${local.project_id}/secrets/${each.key}"
}

import {
  for_each = local.adopted_secret_ids

  to = google_secret_manager_secret_iam_member.runtime_accessor[each.key]
  id = "projects/${local.project_id}/secrets/${each.key} roles/secretmanager.secretAccessor serviceAccount:lucromei-runtime@${local.project_id}.iam.gserviceaccount.com"
}

import {
  to = google_cloud_run_v2_service.test
  id = "projects/${local.project_id}/locations/${local.region}/services/${local.service_name}"
}
