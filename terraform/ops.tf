# Operator tools (python -m lucromei.ops), run on demand with the runtime identity and the
# runtime database role. Billed only while an execution runs. See docs/piloto-roteiro.md.
#
#   gcloud run jobs execute lucromei-ops --region=southamerica-east1 --wait \
#     --args=pilot-check,--hours,2

resource "google_cloud_run_v2_job" "ops" {
  name                = "lucromei-ops"
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
        command = ["python", "-m", "lucromei.ops"]
        args    = ["pilot-check"]

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
          name  = "LUCROMEI_ENVIRONMENT"
          value = "ops"
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
