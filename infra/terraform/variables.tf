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

variable "receiver_s3_bucket" {
  description = "S3 bucket name containing receiver.zip (optional)"
  type        = string
  default     = ""
}

variable "receiver_s3_key" {
  description = "S3 object key for receiver.zip (optional)"
  type        = string
  default     = ""
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

variable "neon_database_url_secret_arn" {
  description = "Secrets Manager ARN for Neon PostgreSQL connection string"
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

variable "shopify_api_version" {
  description = "Explicit Shopify Admin API version"
  type        = string
  default     = "2026-07"
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

variable "theme_resource_types" {
  description = "Comma separated theme resource types to sync (optional)"
  type        = string
  default     = ""
}

variable "global_resource_types" {
  description = "Comma separated non-theme Shopify translatable resource types to sync (optional)"
  type        = string
  default     = ""
}

variable "global_resource_poll_enabled" {
  description = "Enable polling for non-theme Shopify translatable resources"
  type        = string
  default     = "false"
}

variable "theme_id" {
  description = "Approved MAIN theme ID safety pin; writes stop if Shopify MAIN differs"
  type        = string
  default     = ""
}

variable "theme_tracking_enabled" {
  description = "Track MAIN theme file checksums/digests from webhooks; this path is Shopify read-only"
  type        = string
  default     = "false"
}

variable "theme_realtime_sync_enabled" {
  description = "Translate missing/outdated MAIN theme resources after themes/update and themes/publish webhooks"
  type        = string
  default     = "false"
}

variable "theme_poll_enabled" {
  description = "Provision the scheduled theme polling rule"
  type        = string
  default     = "false"
}

variable "scheduled_sync_enabled" {
  description = "Allow the scheduled theme/global poller to execute; keep false until live theme writes are explicitly approved"
  type        = string
  default     = "false"
}

variable "theme_poll_schedule" {
  description = "EventBridge schedule expression for theme polling"
  type        = string
  default     = "cron(0 3 1 * ? *)"
}

variable "catalog_poll_enabled" {
  description = "Enable the incremental product reconciliation schedule"
  type        = string
  default     = "false"
}

variable "catalog_poll_schedule" {
  description = "EventBridge schedule for incremental product reconciliation"
  type        = string
  default     = "rate(15 minutes)"
}

variable "catalog_poll_initial_lookback_hours" {
  description = "Initial updated_at lookback when no catalog checkpoint exists"
  type        = number
  default     = 720
}

variable "catalog_poll_overlap_seconds" {
  description = "Overlap before the prior checkpoint to avoid boundary misses"
  type        = number
  default     = 300
}

variable "catalog_poll_max_pages" {
  description = "Fail-closed page ceiling for a catalog poll invocation"
  type        = number
  default     = 50
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
  default     = 60
}

variable "dry_run" {
  description = "Dry run mode for worker"
  type        = string
  default     = "false"
}

variable "disable_sync" {
  description = "Pause worker processing without acknowledging queued webhook messages"
  type        = string
  default     = "false"
}

variable "log_verbose_sync" {
  description = "Emit verbose Shopify payload debug logs from worker"
  type        = string
  default     = "false"
}

variable "seo_sync_enabled" {
  description = "Maintain custom product meta_title/meta_description translations in the realtime worker"
  type        = string
  default     = "false"
}

variable "openai_model" {
  description = "OpenAI model used consistently by worker and theme/resource poller"
  type        = string
  default     = "gpt-5.6-terra"
}

variable "openai_fallback_model" {
  description = "Optional validation-failure fallback model"
  type        = string
  default     = "gpt-5.6-sol"
}

variable "worker_maximum_concurrency" {
  description = "Maximum concurrent Lambda executions for the SQS worker event source"
  type        = number
  default     = 5

  validation {
    condition     = var.worker_maximum_concurrency >= 2 && var.worker_maximum_concurrency <= 1000
    error_message = "worker_maximum_concurrency must be between 2 and 1000."
  }
}

# Prompt specialization for translator (optional)
variable "translator_specialization" {
  description = "Domain specialization for prompts (for example, 'industrial appliances')"
  type        = string
  default     = ""
}

variable "translator_brand" {
  description = "Optional store or brand name used in translation prompts"
  type        = string
  default     = ""
}

variable "translator_audience" {
  description = "Optional target-audience guidance used in translation prompts"
  type        = string
  default     = "online shoppers"
}

variable "source_language_name" {
  description = "Human label for source language (for example, 'Italian' or 'English')"
  type        = string
  default     = ""
}

variable "do_not_translate_yaml" {
  description = "Packaged path to the do-not-translate and glossary policy"
  type        = string
  default     = "src/config/do_not_translate.yaml"
}

variable "metafield_translation_policy_path" {
  description = "Packaged path to the metafield translation policy"
  type        = string
  default     = "src/config/metafield_translation.yaml"
}

variable "theme_translation_policy_path" {
  description = "Packaged path to the theme translation policy"
  type        = string
  default     = "src/config/theme_translation.yaml"
}
