variable "image" {
  description = "Artifact Registry image pinned to a reviewed digest."
  type        = string
  default     = "southamerica-east1-docker.pkg.dev/lucro-mei/lucromei/lucromei@sha256:0ed5eda34d8c2141632427a0fa0f91164829c08024ef10e5d7479dd4111250f3"

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
        "LUCROMEI_PRIVACY_HASH_KEY",
        "LUCROMEI_WHATSAPP_ACCESS_TOKEN",
        "LUCROMEI_WHATSAPP_APP_SECRET",
        "LUCROMEI_WHATSAPP_VERIFY_TOKEN",
      ], name) && can(regex("^[1-9][0-9]*$", version))
    ])
    error_message = "Specify known Cloud Run secret environment variable names and positive numeric version strings."
  }
}

variable "terms_url" {
  description = "Public https link to the terms of use shown during onboarding."
  type        = string
  default     = "https://lucromei.example/termos-provisorio"

  validation {
    condition     = can(regex("^https://", var.terms_url))
    error_message = "The terms URL must use https."
  }
}
