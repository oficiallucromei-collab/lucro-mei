variable "image" {
  description = "Artifact Registry image pinned to a reviewed digest."
  type        = string
  default     = "southamerica-east1-docker.pkg.dev/lucro-mei/lucromei/lucromei@sha256:cb5c1f17d8059d3a65d94fa39c7e2f54604709601b63daa8e3a032a674c65d80"

  validation {
    condition     = can(regex("^southamerica-east1-docker[.]pkg[.]dev/lucro-mei/lucromei/lucromei@sha256:[a-f0-9]{64}$", var.image))
    error_message = "Use a SHA-256 image digest from the LucroMEI Artifact Registry repository."
  }
}

variable "secret_versions" {
  description = "Overrides for the numbered Secret Manager versions, keyed by Cloud Run environment variable. Defaults: version 1, except those pinned in local.pinned_secret_versions."
  type        = map(string)
  default     = {}

  validation {
    condition = alltrue([
      for name, version in var.secret_versions :
      contains([
        "LUCROMEI_DATABASE_URL",
        "LUCROMEI_INTERNAL_API_KEY",
        "LUCROMEI_MIGRATE_DATABASE_URL",
        "LUCROMEI_PRIVACY_HASH_KEY",
        "LUCROMEI_SENTRY_DSN",
        "LUCROMEI_WHATSAPP_ACCESS_TOKEN",
        "LUCROMEI_WHATSAPP_APP_SECRET",
        "LUCROMEI_WHATSAPP_VERIFY_TOKEN",
      ], name) && can(regex("^[1-9][0-9]*$", version))
    ])
    error_message = "Specify known Cloud Run secret environment variable names and positive numeric version strings."
  }
}

variable "terms_url" {
  description = "Public https link to the terms of use shown during onboarding. The app serves them at /termos."
  type        = string
  default     = "https://lucromei-test-vn7ehb54iq-rj.a.run.app/termos"

  validation {
    condition     = can(regex("^https://", var.terms_url))
    error_message = "The terms URL must use https."
  }
}

variable "contact_email" {
  description = "Support and LGPD contact shown in the published terms and privacy policy."
  type        = string
  default     = "oficiallucromei@gmail.com"
}

variable "notifications_enabled" {
  description = "Send proactive notices (end of trial). Turn on only after the Meta templates are approved."
  type        = bool
  default     = false
}

variable "monitoring_enabled" {
  description = "Create the log metrics, uptime check and alert policies in monitoring.tf. Needs the monitoring roles in iam.tf granted first."
  type        = bool
  default     = false
}

variable "alert_email" {
  description = "Where Cloud Monitoring sends alerts."
  type        = string
  default     = "oficiallucromei@gmail.com"
}

variable "sentry_enabled" {
  description = "Send errors to Sentry. Add the DSN to the lucromei-sentry-dsn secret first (scripts/set-secret.sh)."
  type        = bool
  default     = false
}

variable "max_instances" {
  description = "Maximum instances of the public service. The job queue and maintenance are safe with several; the webhook rate limits are per instance."
  type        = number
  default     = 1
}

variable "separate_worker" {
  description = "Run the job queue in a private always-on service (worker.tf, about US$ 50-70/month) instead of inside the public service."
  type        = bool
  default     = false
}

variable "worker_instances" {
  description = "Maximum instances of the separate worker."
  type        = number
  default     = 1
}
