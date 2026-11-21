variable "enabled" {
  description = "Create the production environment. Creating it costs money (see main.tf); the owner decides."
  type        = bool
  default     = false
}

variable "image" {
  description = "Seed image (digest) for a new service; promotions move it afterwards."
  type        = string
  default     = "southamerica-east1-docker.pkg.dev/lucro-mei/lucromei/lucromei@sha256:908ab2fd36419a24918acf4930fab6662cf0fdc3be174daf24687a75bae79eab"

  validation {
    condition     = can(regex("^southamerica-east1-docker[.]pkg[.]dev/lucro-mei/lucromei/lucromei@sha256:[a-f0-9]{64}$", var.image))
    error_message = "Use a SHA-256 image digest from the LucroMEI Artifact Registry repository."
  }
}

variable "whatsapp_phone_number_id" {
  description = "Phone number ID of the official (verified) WhatsApp number."
  type        = string
  default     = ""
}

variable "terms_url" {
  description = "Public https link to the terms, ideally on the business's own domain."
  type        = string
  default     = "https://lucromei-test-vn7ehb54iq-rj.a.run.app/termos"
}

variable "public_url" {
  description = "Public https address of the production service."
  type        = string
  default     = ""
}

variable "contact_email" {
  type    = string
  default = "oficiallucromei@gmail.com"
}

variable "notifications_enabled" {
  type    = bool
  default = false
}

variable "max_instances" {
  type    = number
  default = 2
}

variable "secret_versions" {
  description = "Pinned Secret Manager versions, keyed by environment variable name."
  type        = map(string)
  default     = {}
}
