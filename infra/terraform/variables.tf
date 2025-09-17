variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "eu-central-1"
}

variable "project" {
  description = "Resource name prefix"
  type        = string
}

variable "receiver_zip" {
  description = "Path to receiver lambda zip"
  type        = string
}

variable "worker_zip" {
  description = "Path to worker lambda zip (leave null when using worker_s3_bucket/key)"
  type        = string
  default     = null
}

# Optional: use S3 for worker code to bypass direct upload size limits
variable "worker_s3_bucket" {
  description = "S3 bucket name containing worker.zip (optional)"
  type        = string
  default     = ""
}

variable "worker_s3_key" {
  description = "S3 object key for worker.zip (optional)"
  type        = string
  default     = ""
}

variable "shopify_webhook_secret" {
  description = "Shopify webhook shared secret (optional; prefer ARN)"
  type        = string
  sensitive   = true
  default     = ""
}

# When using Secrets Manager, pass ARNs below (recommended). If set, Terraform will
# not inject plaintext values and Lambdas will fetch secrets at runtime.
variable "openai_api_key_secret_arn" {
  description = "Secrets Manager ARN for OPENAI API Key"
  type        = string
  default     = ""
}

variable "shopify_admin_token_secret_arn" {
  description = "Secrets Manager ARN for Shopify Admin Token"
  type        = string
  default     = ""
}

variable "shopify_webhook_secret_arn" {
  description = "Secrets Manager ARN for webhook secret"
  type        = string
  default     = ""
}

variable "shop_domain" {
  description = "Shop domain, e.g. myshop.myshopify.com"
  type        = string
}

variable "shopify_admin_token" {
  description = "Admin access token (optional; prefer ARN)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "source_locale" {
  description = "Source locale"
  type        = string
  default     = "en"
}

variable "target_locales" {
  description = "Comma separated target locales"
  type        = string
  default     = "it"
}

variable "mf_include" {
  description = "Comma list of namespace.key to include"
  type        = string
  default     = ""
}

variable "mf_json_paths" {
  description = "Comma list of JSON path rules"
  type        = string
  default     = ""
}

variable "request_timeout" {
  description = "HTTP timeout seconds"
  type        = number
  default     = 30
}

variable "retries" {
  description = "HTTP retries"
  type        = number
  default     = 3
}

variable "debounce_seconds" {
  description = "Debounce window seconds"
  type        = number
  default     = 20
}

variable "dry_run" {
  description = "Dry run mode for worker"
  type        = string
  default     = "false"
}

variable "fill_missing_translations" {
  description = "Backfill missing locale translations on update"
  type        = string
  default     = "false"
}

# Prompt specialization for translator (optional)
variable "translator_specialization" {
  description = "Domain specialization for prompts (e.g., 'elettrodomestici industriali')"
  type        = string
  default     = ""
}

variable "source_language_name" {
  description = "Human label for source language (e.g., 'italiano', 'inglese')"
  type        = string
  default     = ""
}
