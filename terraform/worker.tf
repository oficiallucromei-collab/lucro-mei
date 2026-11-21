# Optional separate worker: a private Cloud Run service that only processes the job queue, so
# the public service only answers webhooks. Off by default (var.separate_worker).
#
# Cost estimate (southamerica-east1, instance-based billing, 1 vCPU / 512 MiB always on):
# about US$ 50-70 per month. With it off, the queue runs inside lucromei-test (no extra cost).
#
# The queue already supports several workers (SELECT ... FOR UPDATE SKIP LOCKED, leases,
# maintenance deduplicated per interval), so the public service can also scale out
# (var.max_instances) with or without this worker.

locals {
  worker = var.separate_worker ? 1 : 0
}

resource "google_cloud_run_v2_service" "worker" {
  count = local.worker

  name                = "${local.service_name}-worker"
  location            = local.region
  ingress             = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  deletion_protection = true

  lifecycle {
    ignore_changes = [client, client_version, template[0].containers[0].image]
  }

  scaling {
    min_instance_count = 1
    max_instance_count = var.worker_instances
  }

  template {
    service_account = google_service_account.runtime.email

    containers {
      name  = "lucromei-worker"
      image = var.image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1000m"
          memory = "512Mi"
        }
        cpu_idle = false
      }

      dynamic "env" {
        for_each = {
          LUCROMEI_ENVIRONMENT              = "production"
          LUCROMEI_WORKER_MODE              = "embedded"
          LUCROMEI_WHATSAPP_PHONE_NUMBER_ID = "1434742236396843"
          SSL_CERT_FILE                     = "/etc/ssl/certs/ca-certificates.crt"
          LUCROMEI_TERMS_URL                = var.terms_url
          LUCROMEI_CONTACT_EMAIL            = var.contact_email
          LUCROMEI_NOTIFICATIONS_ENABLED    = tostring(var.notifications_enabled)
        }

        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = merge(local.secret_env, local.optional_secret_env)

        content {
          name = env.key

          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.app[env.value].secret_id
              version = lookup(local.secret_versions, env.key, "1")
            }
          }
        }
      }
    }
  }

  depends_on = [google_secret_manager_secret_iam_member.runtime_accessor]
}

# Lets the release pipeline move the worker to each new image (cloudbuild.yaml).
resource "google_cloud_run_v2_service_iam_member" "build_worker_deployer" {
  count = local.worker

  name     = google_cloud_run_v2_service.worker[0].name
  location = local.region
  role     = "roles/run.developer"
  member   = "serviceAccount:${google_service_account.build.email}"
}
