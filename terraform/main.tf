terraform {
  required_version = ">= 1.7"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.0"
    }
  }

  # State lives in a private, versioned bucket created outside this configuration
  # (gsutil-created; not managed here to avoid a chicken-and-egg dependency).
  backend "gcs" {
    bucket = "lucro-mei-tfstate-312975377897"
    prefix = "lucromei"
  }
}

provider "google" {
  project = local.project_id
  region  = local.region
}

locals {
  project_id   = "lucro-mei"
  region       = "southamerica-east1"
  service_name = "lucromei-test"

  apis = toset([
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "iam.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
  ])

  secret_env = {
    LUCROMEI_DATABASE_URL          = "lucromei-test-database-url"
    LUCROMEI_WHATSAPP_ACCESS_TOKEN = "lucromei-whatsapp-access-token"
    LUCROMEI_WHATSAPP_APP_SECRET   = "lucromei-whatsapp-app-secret"
    LUCROMEI_WHATSAPP_VERIFY_TOKEN = "lucromei-whatsapp-verify-token"
    LUCROMEI_INTERNAL_API_KEY      = "lucromei-internal-api-key"
    # HMAC key for the anonymous proof kept after an LGPD deletion. Payload added in Secret
    # Manager, never in Terraform.
    LUCROMEI_PRIVACY_HASH_KEY = "lucromei-privacy-hash-key"
  }

  # Versions in use when they are not 1. var.secret_versions overrides these per release.
  pinned_secret_versions = {
    LUCROMEI_WHATSAPP_ACCESS_TOKEN = "3"
  }
  secret_versions = merge(local.pinned_secret_versions, var.secret_versions)

  # Owner credentials for schema migrations, used only by the migration job: the app's own
  # database role cannot change the schema.
  migrate_database_secret = "lucromei-test-migrate-database-url"

  # Optional secrets: created empty here, filled by the owner with scripts/set-secret.sh, and
  # wired into the service only when their feature flag is on.
  optional_secret_env = merge(
    var.sentry_enabled ? { LUCROMEI_SENTRY_DSN = "lucromei-sentry-dsn" } : {},
  )

  secret_ids = toset(concat(
    values(local.secret_env),
    ["lucromei-database-url", local.migrate_database_secret, "lucromei-sentry-dsn"],
  ))
}

resource "google_project_service" "api" {
  for_each = local.apis

  service            = each.key
  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "app" {
  location      = local.region
  repository_id = "lucromei"
  format        = "DOCKER"
  description   = "LucroMEI container releases"

  cleanup_policy_dry_run = false

  cleanup_policies {
    id     = "keep-recent-10"
    action = "KEEP"
    most_recent_versions {
      keep_count = 10
    }
  }

  cleanup_policies {
    id     = "delete-untagged-14d"
    action = "DELETE"
    condition {
      tag_state  = "UNTAGGED"
      older_than = "1209600s"
    }
  }

  cleanup_policies {
    id     = "delete-older-90d"
    action = "DELETE"
    condition {
      tag_state  = "ANY"
      older_than = "7776000s"
    }
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [google_project_service.api["artifactregistry.googleapis.com"]]
}

resource "google_service_account" "runtime" {
  account_id   = "lucromei-runtime"
  display_name = "LucroMEI Cloud Run runtime"

  depends_on = [google_project_service.api["iam.googleapis.com"]]
}

resource "google_secret_manager_secret" "app" {
  for_each = local.secret_ids

  secret_id = each.key

  replication {
    dynamic "auto" {
      for_each = each.key == local.secret_env.LUCROMEI_DATABASE_URL ? [true] : []

      content {}
    }

    dynamic "user_managed" {
      for_each = each.key == local.secret_env.LUCROMEI_DATABASE_URL ? [] : [local.region]

      content {
        replicas {
          location = user_managed.value
        }
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [google_project_service.api["secretmanager.googleapis.com"]]
}

resource "google_secret_manager_secret_iam_member" "runtime_accessor" {
  for_each = google_secret_manager_secret.app

  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_cloud_run_v2_service" "test" {
  name                 = local.service_name
  location             = local.region
  ingress              = "INGRESS_TRAFFIC_ALL"
  invoker_iam_disabled = true
  deletion_protection  = true

  lifecycle {
    # client fields: set by whichever client (console, gcloud) last touched the service.
    # image: releases move it from cloudbuild.yaml; var.image only seeds a new service.
    ignore_changes = [client, client_version, template[0].containers[0].image]
  }

  scaling {
    min_instance_count = 0
    max_instance_count = var.max_instances
  }

  template {
    service_account = google_service_account.runtime.email

    containers {
      name  = "lucromei-1"
      image = var.image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1000m"
          memory = "512Mi"
        }
        # The embedded job worker needs CPU between requests (instance-based billing).
        cpu_idle          = false
        startup_cpu_boost = true
      }

      env {
        name  = "LUCROMEI_ENVIRONMENT"
        value = "production"
      }

      env {
        name  = "LUCROMEI_WHATSAPP_PHONE_NUMBER_ID"
        value = "1434742236396843"
      }

      env {
        # "off" when the separate worker (worker.tf) processes the queue.
        name  = "LUCROMEI_WORKER_MODE"
        value = var.separate_worker ? "off" : "embedded"
      }

      env {
        name  = "SSL_CERT_FILE"
        value = "/etc/ssl/certs/ca-certificates.crt"
      }

      env {
        # The app publishes the terms at /termos and the privacy policy at /privacidade.
        name  = "LUCROMEI_TERMS_URL"
        value = var.terms_url
      }

      env {
        name  = "LUCROMEI_CONTACT_EMAIL"
        value = var.contact_email
      }

      env {
        # Proactive notices need the templates in docs/whatsapp-templates.md approved at Meta.
        name  = "LUCROMEI_NOTIFICATIONS_ENABLED"
        value = tostring(var.notifications_enabled)
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

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  depends_on = [
    google_project_service.api["run.googleapis.com"],
    google_artifact_registry_repository.app,
    google_secret_manager_secret_iam_member.runtime_accessor,
  ]
}

output "test_webhook_url" {
  description = "Public WhatsApp callback URL for the test service."
  value       = "${google_cloud_run_v2_service.test.uri}/webhooks/whatsapp"
}
