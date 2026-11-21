# Grants that need IAM admin rights (project or service account setIamPolicy). Planning them
# needs getIamPolicy on the project and on lucromei-runtime; applying them needs an IAM admin.

locals {
  cloudbuild_service_agent = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-cloudbuild.iam.gserviceaccount.com"
}

data "google_project" "this" {
  project_id = local.project_id
}

# Lets Cloud Build write logs as lucromei-build (cloudbuild.yaml uses CLOUD_LOGGING_ONLY).
resource "google_project_iam_member" "build_log_writer" {
  project = local.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.build.email}"
}

# The service and job run as lucromei-runtime, so deploying them needs actAs on it.
resource "google_service_account_iam_member" "build_acts_as_runtime" {
  service_account_id = google_service_account.runtime.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.build.email}"
}

# Lets the Cloud Build service agent store the lucromei-github connection's OAuth token.
resource "google_project_iam_member" "cloudbuild_agent_secret_admin" {
  project = local.project_id
  role    = "roles/secretmanager.admin"
  member  = local.cloudbuild_service_agent
}

# Project roles of the account that operates this project (Terraform, gcloud, releases).
# roles/owner is granted only temporarily by hand and is deliberately not tracked.
# Changing these needs an IAM admin; this account cannot grant itself roles.
locals {
  operator = "user:oficiallucromei@gmail.com"

  operator_roles = toset([
    "roles/artifactregistry.admin",
    "roles/cloudbuild.builds.editor",
    "roles/cloudbuild.connectionAdmin", # create the GitHub connection and repository link
    "roles/iam.securityReviewer",       # read IAM policies, so plans of this file work
    "roles/iam.serviceAccountCreator",
    "roles/iam.serviceAccountUser",
    "roles/logging.configWriter", # log-based metrics (monitoring.tf)
    "roles/logging.logWriter",
    "roles/logging.viewer",
    "roles/monitoring.editor", # alert policies, uptime check, notification channel
    "roles/run.admin",
    "roles/secretmanager.admin",
    "roles/serviceusage.serviceUsageConsumer",
    "roles/storage.admin",
  ])
}

resource "google_project_iam_member" "operator" {
  for_each = local.operator_roles

  project = local.project_id
  role    = each.key
  member  = local.operator
}
