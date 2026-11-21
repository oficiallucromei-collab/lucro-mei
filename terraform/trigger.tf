# Runs cloudbuild.yaml (test, build, smoke test, push, migrate, deploy) on every push to main.

# Created by hand once (the GitHub authorization is interactive) and imported; see README.
# Cloud Build's service agent created the OAuth token secret and keeps it up to date.
resource "google_cloudbuildv2_connection" "github" {
  name     = "lucromei-github"
  location = local.region

  github_config {
    app_installation_id = 183528323

    authorizer_credential {
      oauth_token_secret_version = "projects/lucro-mei/locations/southamerica-east1/secrets/lucromei-github-github-oauthtoken-af8e8a/versions/latest"
    }
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [google_project_iam_member.cloudbuild_agent_secret_admin]
}

resource "google_cloudbuildv2_repository" "app" {
  name              = "lucro-mei"
  location          = local.region
  parent_connection = google_cloudbuildv2_connection.github.name
  remote_uri        = "https://github.com/oficiallucromei-collab/lucro-mei.git"
}

resource "google_cloudbuild_trigger" "release" {
  name            = "lucromei-release"
  location        = local.region
  description     = "Test, build and deploy lucromei-test from main"
  filename        = "cloudbuild.yaml"
  service_account = google_service_account.build.id

  repository_event_config {
    repository = google_cloudbuildv2_repository.app.id

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
