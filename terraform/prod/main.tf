# Production environment: lucromei-prod. Separate Terraform state (prefix lucromei-prod), its
# own runtime identity, secrets, migration and ops jobs, and a promotion trigger that needs a
# manual approval in Cloud Build. NOTHING is created while var.enabled is false (the default).
#
# Promotion: the image already tested in lucromei-test (same digest) is migrated and deployed
# to production by cloudbuild-promote.yaml; nothing is rebuilt. See docs/producao.md.
#
# Monthly cost estimate with enabled = true (southamerica-east1, low volume):
#   Cloud Run, 1 always-on instance (1 vCPU, 512 MiB, instance billing) ...... US$ 50-70
#   Secret Manager (about 9 active versions) ................................. US$ 0.54
#   Cloud Build promotions (a few minutes per release) ....................... ~US$ 0
#   Neon production project (outside Terraform; Launch plan) ................. ~US$ 19+
#   WhatsApp (Meta): service replies within 24 h are free; utility templates in Brazil are
#   charged per message (about US$ 0.008 each, check current Meta pricing).

terraform {
  required_version = ">= 1.7"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.0"
    }
  }

  backend "gcs" {
    bucket = "lucro-mei-tfstate-312975377897"
    prefix = "lucromei-prod"
  }
}

provider "google" {
  project = local.project_id
  region  = local.region
}

locals {
  project_id = "lucro-mei"
  region     = "southamerica-east1"
  service    = "lucromei-prod"
  on         = var.enabled ? 1 : 0

  # Production secrets; values are added by the owner with scripts/set-secret.sh.
  secret_env = {
    LUCROMEI_DATABASE_URL          = "lucromei-prod-database-url"
    LUCROMEI_WHATSAPP_ACCESS_TOKEN = "lucromei-prod-whatsapp-access-token"
    LUCROMEI_WHATSAPP_APP_SECRET   = "lucromei-prod-whatsapp-app-secret"
    LUCROMEI_WHATSAPP_VERIFY_TOKEN = "lucromei-prod-whatsapp-verify-token"
    LUCROMEI_PRIVACY_HASH_KEY      = "lucromei-prod-privacy-hash-key"
  }
  migrate_secret = "lucromei-prod-migrate-database-url"
  secret_ids     = var.enabled ? toset(concat(values(local.secret_env), [local.migrate_secret])) : toset([])

  plain_env = {
    LUCROMEI_ENVIRONMENT              = "production"
    LUCROMEI_WHATSAPP_PHONE_NUMBER_ID = var.whatsapp_phone_number_id
    LUCROMEI_TERMS_URL                = var.terms_url
    LUCROMEI_CONTACT_EMAIL            = var.contact_email
    LUCROMEI_PUBLIC_URL               = var.public_url
    LUCROMEI_NOTIFICATIONS_ENABLED    = tostring(var.notifications_enabled)
    SSL_CERT_FILE                     = "/etc/ssl/certs/ca-certificates.crt"
  }
}

resource "google_service_account" "runtime" {
  count        = local.on
  account_id   = "lucromei-prod-runtime"
  display_name = "LucroMEI production runtime"
}

resource "google_secret_manager_secret" "app" {
  for_each  = local.secret_ids
  secret_id = each.key

  replication {
    user_managed {
      replicas {
        location = local.region
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_secret_manager_secret_iam_member" "runtime" {
  for_each  = google_secret_manager_secret.app
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime[0].email}"
}

resource "google_cloud_run_v2_service" "prod" {
  count                = local.on
  name                 = local.service
  location             = local.region
  ingress              = "INGRESS_TRAFFIC_ALL"
  invoker_iam_disabled = true
  deletion_protection  = true

  lifecycle {
    ignore_changes = [client, client_version, template[0].containers[0].image]
  }

  scaling {
    # One warm instance: no cold start for the first message, and the embedded worker runs.
    min_instance_count = 1
    max_instance_count = var.max_instances
  }

  template {
    service_account = google_service_account.runtime[0].email

    containers {
      image = var.image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1000m"
          memory = "512Mi"
        }
        cpu_idle          = false
        startup_cpu_boost = true
      }

      dynamic "env" {
        for_each = local.plain_env
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.secret_env
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.app[env.value].secret_id
              version = lookup(var.secret_versions, env.key, "1")
            }
          }
        }
      }
    }
  }

  depends_on = [google_secret_manager_secret_iam_member.runtime]
}

resource "google_cloud_run_v2_job" "migrate" {
  count               = local.on
  name                = "lucromei-prod-migrate"
  location            = local.region
  deletion_protection = false

  lifecycle {
    ignore_changes = [client, client_version, template[0].template[0].containers[0].image]
  }

  template {
    template {
      service_account = google_service_account.runtime[0].email
      max_retries     = 0
      timeout         = "300s"

      containers {
        image   = var.image
        command = ["alembic"]
        args    = ["upgrade", "head"]

        env {
          name  = "SSL_CERT_FILE"
          value = "/etc/ssl/certs/ca-certificates.crt"
        }

        env {
          name = "LUCROMEI_DATABASE_URL"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.app[local.migrate_secret].secret_id
              version = lookup(var.secret_versions, "LUCROMEI_MIGRATE_DATABASE_URL", "1")
            }
          }
        }
      }
    }
  }

  depends_on = [google_secret_manager_secret_iam_member.runtime]
}

resource "google_cloud_run_v2_job" "ops" {
  count               = local.on
  name                = "lucromei-prod-ops"
  location            = local.region
  deletion_protection = false

  lifecycle {
    ignore_changes = [client, client_version, template[0].template[0].containers[0].image]
  }

  template {
    template {
      service_account = google_service_account.runtime[0].email
      max_retries     = 0
      timeout         = "300s"

      containers {
        image   = var.image
        command = ["python", "-m", "lucromei.ops"]
        args    = ["pilot-check"]

        env {
          name  = "SSL_CERT_FILE"
          value = "/etc/ssl/certs/ca-certificates.crt"
        }

        env {
          name = "LUCROMEI_DATABASE_URL"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.app[local.secret_env.LUCROMEI_DATABASE_URL].secret_id
              version = lookup(var.secret_versions, "LUCROMEI_DATABASE_URL", "1")
            }
          }
        }
      }
    }
  }

  depends_on = [google_secret_manager_secret_iam_member.runtime]
}

# The release build account (managed in ../build.tf) deploys production only through the
# promotion trigger below, which waits for a manual approval in the Cloud Build console.
data "google_service_account" "build" {
  account_id = "lucromei-build"
}

resource "google_cloud_run_v2_service_iam_member" "build_deployer" {
  count    = local.on
  name     = google_cloud_run_v2_service.prod[0].name
  location = local.region
  role     = "roles/run.developer"
  member   = "serviceAccount:${data.google_service_account.build.email}"
}

resource "google_cloud_run_v2_job_iam_member" "build_jobs" {
  for_each = var.enabled ? toset(["lucromei-prod-migrate", "lucromei-prod-ops"]) : toset([])
  name     = each.key
  location = local.region
  role     = "roles/run.developer"
  member   = "serviceAccount:${data.google_service_account.build.email}"

  depends_on = [google_cloud_run_v2_job.migrate, google_cloud_run_v2_job.ops]
}

resource "google_service_account_iam_member" "build_acts_as_runtime" {
  count              = local.on
  service_account_id = google_service_account.runtime[0].name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${data.google_service_account.build.email}"
}

resource "google_cloudbuild_trigger" "promote" {
  count           = local.on
  name            = "lucromei-promote"
  location        = local.region
  description     = "Promote the image running in lucromei-test to lucromei-prod (needs approval)"
  service_account = data.google_service_account.build.id

  source_to_build {
    repository = "projects/${local.project_id}/locations/${local.region}/connections/lucromei-github/repositories/lucro-mei"
    ref        = "refs/heads/main"
    repo_type  = "GITHUB"
  }

  git_file_source {
    path       = "cloudbuild-promote.yaml"
    repository = "projects/${local.project_id}/locations/${local.region}/connections/lucromei-github/repositories/lucro-mei"
    revision   = "refs/heads/main"
    repo_type  = "GITHUB"
  }

  approval_config {
    approval_required = true
  }
}

output "prod_webhook_url" {
  value = var.enabled ? "${google_cloud_run_v2_service.prod[0].uri}/webhooks/whatsapp" : null
}
