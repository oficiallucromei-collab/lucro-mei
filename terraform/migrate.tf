# Runs `alembic upgrade head` from the release image against the service's database, with the
# runtime identity. A release first points this job at the new image (var.migration_image),
# executes it, and only then moves the service to that image (var.image). See README.

variable "migration_image" {
  description = "Image the migration job runs. Empty means the service image (var.image)."
  type        = string
  default     = ""

  validation {
    condition     = var.migration_image == "" || can(regex("^southamerica-east1-docker[.]pkg[.]dev/lucro-mei/lucromei/lucromei@sha256:[a-f0-9]{64}$", var.migration_image))
    error_message = "Use a SHA-256 image digest from the LucroMEI Artifact Registry repository."
  }
}

resource "google_cloud_run_v2_job" "migrate" {
  name                = "lucromei-migrate"
  location            = local.region
  deletion_protection = false

  lifecycle {
    ignore_changes = [client, client_version]
  }

  template {
    task_count = 1

    template {
      service_account = google_service_account.runtime.email
      max_retries     = 0
      timeout         = "300s"

      containers {
        image   = coalesce(var.migration_image, var.image)
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
              secret  = google_secret_manager_secret.app[local.secret_env.LUCROMEI_DATABASE_URL].secret_id
              version = lookup(local.secret_versions, "LUCROMEI_DATABASE_URL", "1")
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
