resource "google_project_service" "build_provenance" {
  service            = "containeranalysis.googleapis.com"
  disable_on_destroy = false
}

resource "google_service_account" "build" {
  account_id   = "lucromei-build"
  display_name = "LucroMEI Cloud Build release publisher"

  depends_on = [google_project_service.api["iam.googleapis.com"]]
}

resource "google_artifact_registry_repository_iam_member" "build_writer" {
  location   = google_artifact_registry_repository.app.location
  repository = google_artifact_registry_repository.app.repository_id
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.build.email}"
}

# Project-level grants (roles/logging.logWriter for the build account) need a project IAM
# admin and are applied by the project owner outside Terraform; see README.

resource "google_storage_bucket" "build_source" {
  name                        = "${local.project_id}-release-source-312975377897"
  location                    = local.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  lifecycle_rule {
    action {
      type = "Delete"
    }

    condition {
      age = 7
    }
  }
}

resource "google_storage_bucket_iam_member" "build_source_reader" {
  bucket = google_storage_bucket.build_source.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.build.email}"
}

output "build_source_bucket" {
  description = "Private staging bucket for reviewed release source archives."
  value       = google_storage_bucket.build_source.url
}
