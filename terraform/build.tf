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

# Grants that need an IAM admin (log writing, actAs on lucromei-runtime) are in iam.tf.

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

# Deploy rights for cloudbuild.yaml, scoped to the one service and job it releases.
resource "google_cloud_run_v2_service_iam_member" "build_deployer" {
  name     = google_cloud_run_v2_service.test.name
  location = google_cloud_run_v2_service.test.location
  role     = "roles/run.developer"
  member   = "serviceAccount:${google_service_account.build.email}"
}

resource "google_cloud_run_v2_job_iam_member" "build_migrator" {
  name     = google_cloud_run_v2_job.migrate.name
  location = google_cloud_run_v2_job.migrate.location
  role     = "roles/run.developer"
  member   = "serviceAccount:${google_service_account.build.email}"
}

output "build_source_bucket" {
  description = "Private staging bucket for reviewed release source archives."
  value       = google_storage_bucket.build_source.url
}
