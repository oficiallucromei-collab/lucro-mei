# Runs cloudbuild.yaml (test, build, smoke test, push, migrate, deploy) on every push to main.
#
# The GitHub connection is created outside Terraform because it needs an interactive GitHub
# authorization; see README. Set release_trigger_enabled = true once it exists.

variable "release_trigger_enabled" {
  description = "Create the push-to-main release trigger. Needs the lucromei-github connection."
  type        = bool
  default     = false
}

locals {
  github_connection = "projects/${local.project_id}/locations/${local.region}/connections/lucromei-github"
}

resource "google_cloudbuildv2_repository" "app" {
  count = var.release_trigger_enabled ? 1 : 0

  name              = "lucro-mei"
  location          = local.region
  parent_connection = local.github_connection
  remote_uri        = "https://github.com/oficiallucromei-collab/lucro-mei.git"
}

resource "google_cloudbuild_trigger" "release" {
  count = var.release_trigger_enabled ? 1 : 0

  name            = "lucromei-release"
  location        = local.region
  description     = "Test, build and deploy lucromei-test from main"
  filename        = "cloudbuild.yaml"
  service_account = google_service_account.build.id

  repository_event_config {
    repository = google_cloudbuildv2_repository.app[0].id

    push {
      branch = "^main$"
    }
  }

  depends_on = [
    google_cloud_run_v2_service_iam_member.build_deployer,
    google_cloud_run_v2_job_iam_member.build_migrator,
    google_project_iam_member.build_log_writer,
    google_service_account_iam_member.build_acts_as_runtime,
  ]
}
