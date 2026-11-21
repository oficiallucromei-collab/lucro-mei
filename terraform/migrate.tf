# Runs `alembic upgrade head` from the release image against the service's database, with the
# runtime identity. cloudbuild.yaml points this job at the new image and executes it before it
# moves the service to that image. See README.

resource "google_cloud_run_v2_job" "migrate" {
  name                = "lucromei-migrate"
  location            = local.region
  deletion_protection = false

  lifecycle {
    # image: releases move it from cloudbuild.yaml; var.image only seeds a new job.
    ignore_changes = [client, client_version, template[0].template[0].containers[0].image]
  }

  template {
    task_count = 1

    template {
      service_account = google_service_account.runtime.email
      max_retries     = 0
      timeout         = "300s"

      containers {
        image   = var.image
        command = ["alembic"]
        args    = ["upgrade", "head"]

        resources {
          limits = {
            cpu    = "1000m"
            memory = "512Mi"
          }
        }

        env {
          name  = "SSL_CERT_FILE"
          value = "/etc/ssl/certs/ca-certificates.crt"
        }

        env {
          name = "LUCROMEI_DATABASE_URL"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.app[local.migrate_database_secret].secret_id
              version = "1"
            }
          }
        }
      }
    }
  }

  depends_on = [
    google_project_service.api["run.googleapis.com"],
    google_secret_manager_secret_iam_member.runtime_accessor,
  ]
}
