terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

locals {
  name_prefix          = "${var.project}-shopify"
  theme_poll_enabled   = var.theme_poll_enabled == "true"
  catalog_poll_enabled = var.catalog_poll_enabled == "true"
}

resource "aws_sqs_queue" "products_dlq" {
  name                      = "${local.name_prefix}-products-dlq"
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue" "products" {
  name                       = "${local.name_prefix}-products-queue"
  visibility_timeout_seconds = 1800
  message_retention_seconds  = 1209600
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.products_dlq.arn
    maxReceiveCount     = 5
  })
}

resource "aws_dynamodb_table" "product_snapshots" {
  name         = "${local.name_prefix}-product-snapshots"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }
}

resource "aws_dynamodb_table" "webhook_dedup" {
  name         = "${local.name_prefix}-webhook-dedup"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "event_id"

  attribute {
    name = "event_id"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}

data "aws_iam_policy_document" "receiver_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "receiver" {
  name               = "${local.name_prefix}-receiver-role"
  assume_role_policy = data.aws_iam_policy_document.receiver_assume.json
}

resource "aws_iam_role_policy" "receiver" {
  name = "${local.name_prefix}-receiver-policy"
  role = aws_iam_role.receiver.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Effect   = "Allow",
        Action   = ["sqs:SendMessage"],
        Resource = aws_sqs_queue.products.arn
      },
      {
        Effect   = "Allow",
        Action   = ["secretsmanager:GetSecretValue"],
        Resource = var.shopify_webhook_secret_arn != "" ? var.shopify_webhook_secret_arn : null
      },
      {
        Effect   = "Allow",
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
        Resource = "*"
      }
    ]
  })
}

resource "aws_lambda_function" "receiver" {
  function_name    = "${local.name_prefix}-webhook-receiver"
  role             = aws_iam_role.receiver.arn
  handler          = "src/aws_lambda/receiver.handler"
  runtime          = "python3.12"
  filename         = (var.receiver_s3_bucket == "" && var.receiver_s3_key == "") ? var.receiver_zip : null
  s3_bucket        = var.receiver_s3_bucket != "" ? var.receiver_s3_bucket : null
  s3_key           = var.receiver_s3_key != "" ? var.receiver_s3_key : null
  source_code_hash = (var.receiver_s3_bucket == "" && var.receiver_s3_key == "") ? filebase64sha256(var.receiver_zip) : null
  environment {
    variables = {
      SQS_URL                    = aws_sqs_queue.products.id
      SHOPIFY_WEBHOOK_SECRET_ARN = var.shopify_webhook_secret_arn
      DISABLE_SYNC               = var.disable_sync
    }
  }
}

resource "aws_lambda_function_url" "receiver" {
  function_name      = aws_lambda_function.receiver.function_name
  authorization_type = "NONE"
  cors {
    allow_origins = ["*"]
    # Shopify calls via server-to-server POST; OPTIONS is invalid for Function URL CORS (max len 6)
    allow_methods = ["POST"]
  }
}

data "aws_iam_policy_document" "worker_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "worker" {
  name               = "${local.name_prefix}-worker-role"
  assume_role_policy = data.aws_iam_policy_document.worker_assume.json
}

resource "aws_iam_role_policy" "worker" {
  name = "${local.name_prefix}-worker-policy"
  role = aws_iam_role.worker.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Effect = "Allow",
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:ChangeMessageVisibility",
          "sqs:GetQueueAttributes"
        ],
        Resource = aws_sqs_queue.products.arn
      },
      {
        Effect = "Allow",
        Action = ["secretsmanager:GetSecretValue"],
        Resource = compact([
          var.openai_api_key_secret_arn,
          var.shopify_admin_token_secret_arn,
          var.neon_database_url_secret_arn
        ])
      },
      {
        Effect   = "Allow",
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"],
        Resource = [aws_dynamodb_table.product_snapshots.arn, aws_dynamodb_table.webhook_dedup.arn]
      },
      {
        Effect   = "Allow",
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
        Resource = "*"
      }
    ]
  })
}

resource "aws_lambda_function" "worker" {
  function_name = "${local.name_prefix}-webhook-worker"
  role          = aws_iam_role.worker.arn
  handler       = "src/aws_lambda/worker.handler"
  runtime       = "python3.12"
  # Use local filename by default; switch to S3 when variables provided to bypass 70MB API limit
  filename  = (var.worker_s3_bucket == "" && var.worker_s3_key == "") ? var.worker_zip : null
  s3_bucket = var.worker_s3_bucket != "" ? var.worker_s3_bucket : null
  s3_key    = var.worker_s3_key != "" ? var.worker_s3_key : null
  # Only compute hash when using local file
  source_code_hash = (var.worker_s3_bucket == "" && var.worker_s3_key == "") ? filebase64sha256(var.worker_zip) : null
  timeout          = 300
  memory_size      = 512
  environment {
    variables = {
      DDB_TABLE           = aws_dynamodb_table.product_snapshots.name
      DEDUP_TABLE         = aws_dynamodb_table.webhook_dedup.name
      SQS_URL             = aws_sqs_queue.products.id
      SOURCE_LOCALE       = var.source_locale
      TARGET_LOCALES      = var.target_locales
      MF_INCLUDE          = var.mf_include
      SHOP_DOMAIN         = var.shop_domain
      SHOPIFY_API_VERSION = var.shopify_api_version
      # Secrets Manager ARNs for runtime retrieval
      OPENAI_API_KEY_SECRET_ARN      = var.openai_api_key_secret_arn
      SHOPIFY_ADMIN_TOKEN_SECRET_ARN = var.shopify_admin_token_secret_arn
      NEON_DATABASE_URL_SECRET_ARN   = var.neon_database_url_secret_arn
      REQUEST_TIMEOUT                = var.request_timeout
      RETRIES                        = var.retries
      DEBOUNCE_SECONDS               = var.debounce_seconds
      DRY_RUN                        = var.dry_run
      DISABLE_SYNC                   = var.disable_sync
      LOG_VERBOSE_SYNC               = var.log_verbose_sync
      SEO_SYNC_ENABLED               = var.seo_sync_enabled
      THEME_TRACKING_ENABLED         = var.theme_tracking_enabled
      THEME_REALTIME_SYNC_ENABLED    = var.theme_realtime_sync_enabled
      APPROVED_THEME_ID              = var.theme_id
      OPENAI_MODEL                   = var.openai_model
      OPENAI_FALLBACK_MODEL          = var.openai_fallback_model
      # Prompt specialization (optional)
      TRANSLATOR_SPECIALIZATION         = var.translator_specialization
      TRANSLATOR_BRAND                  = var.translator_brand
      TRANSLATOR_AUDIENCE               = var.translator_audience
      SOURCE_LANGUAGE_NAME              = var.source_language_name
      DO_NOT_TRANSLATE_YAML             = var.do_not_translate_yaml
      METAFIELD_TRANSLATION_POLICY_PATH = var.metafield_translation_policy_path
      THEME_TRANSLATION_POLICY_PATH     = var.theme_translation_policy_path
    }
  }
}

resource "aws_lambda_event_source_mapping" "worker_sqs" {
  event_source_arn        = aws_sqs_queue.products.arn
  function_name           = aws_lambda_function.worker.arn
  batch_size              = 1
  enabled                 = var.disable_sync != "true"
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = var.worker_maximum_concurrency
  }
}

resource "aws_iam_role" "catalog_poller" {
  name               = "${local.name_prefix}-catalog-poller-role"
  assume_role_policy = data.aws_iam_policy_document.worker_assume.json
}

resource "aws_iam_role_policy" "catalog_poller" {
  name = "${local.name_prefix}-catalog-poller-policy"
  role = aws_iam_role.catalog_poller.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Effect   = "Allow",
        Action   = ["secretsmanager:GetSecretValue"],
        Resource = var.shopify_admin_token_secret_arn
      },
      {
        Effect   = "Allow",
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem"],
        Resource = aws_dynamodb_table.product_snapshots.arn
      },
      {
        Effect   = "Allow",
        Action   = ["sqs:SendMessage"],
        Resource = aws_sqs_queue.products.arn
      },
      {
        Effect   = "Allow",
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
        Resource = "*"
      }
    ]
  })
}

resource "aws_lambda_function" "catalog_poller" {
  function_name    = "${local.name_prefix}-catalog-poller"
  role             = aws_iam_role.catalog_poller.arn
  handler          = "src/aws_lambda/catalog_poller.handler"
  runtime          = "python3.12"
  filename         = (var.worker_s3_bucket == "" && var.worker_s3_key == "") ? var.worker_zip : null
  s3_bucket        = var.worker_s3_bucket != "" ? var.worker_s3_bucket : null
  s3_key           = var.worker_s3_key != "" ? var.worker_s3_key : null
  source_code_hash = (var.worker_s3_bucket == "" && var.worker_s3_key == "") ? filebase64sha256(var.worker_zip) : null
  timeout          = 120
  memory_size      = 256
  environment {
    variables = {
      CATALOG_POLL_ENABLED                = var.disable_sync == "true" ? "false" : var.catalog_poll_enabled
      CATALOG_POLL_INITIAL_LOOKBACK_HOURS = var.catalog_poll_initial_lookback_hours
      CATALOG_POLL_OVERLAP_SECONDS        = var.catalog_poll_overlap_seconds
      CATALOG_POLL_MAX_PAGES              = var.catalog_poll_max_pages
      SHOP_DOMAIN                         = var.shop_domain
      SHOPIFY_API_VERSION                 = var.shopify_api_version
      SHOPIFY_ADMIN_TOKEN_SECRET_ARN      = var.shopify_admin_token_secret_arn
      DDB_TABLE                           = aws_dynamodb_table.product_snapshots.name
      SQS_URL                             = aws_sqs_queue.products.id
      DRY_RUN                             = var.dry_run
      REQUEST_TIMEOUT                     = var.request_timeout
      RETRIES                             = var.retries
    }
  }
}

resource "aws_cloudwatch_event_rule" "catalog_poll" {
  count               = local.catalog_poll_enabled ? 1 : 0
  name                = "${local.name_prefix}-catalog-poll"
  schedule_expression = var.catalog_poll_schedule
}

resource "aws_cloudwatch_event_target" "catalog_poll" {
  count = local.catalog_poll_enabled ? 1 : 0
  rule  = aws_cloudwatch_event_rule.catalog_poll[0].name
  arn   = aws_lambda_function.catalog_poller.arn
}

resource "aws_lambda_permission" "catalog_poll" {
  count         = local.catalog_poll_enabled ? 1 : 0
  statement_id  = "AllowEventBridgeInvokeCatalogPoller"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.catalog_poller.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.catalog_poll[0].arn
}

resource "aws_iam_role" "theme_poller" {
  name               = "${local.name_prefix}-theme-poller-role"
  assume_role_policy = data.aws_iam_policy_document.worker_assume.json
}

resource "aws_iam_role_policy" "theme_poller" {
  name = "${local.name_prefix}-theme-poller-policy"
  role = aws_iam_role.theme_poller.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Effect = "Allow",
        Action = ["secretsmanager:GetSecretValue"],
        Resource = compact([
          var.openai_api_key_secret_arn,
          var.shopify_admin_token_secret_arn,
          var.neon_database_url_secret_arn
        ])
      },
      {
        Effect   = "Allow",
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
        Resource = "*"
      }
    ]
  })
}

resource "aws_lambda_function" "theme_poller" {
  function_name    = "${local.name_prefix}-theme-poller"
  role             = aws_iam_role.theme_poller.arn
  handler          = "src/aws_lambda/theme_poller.handler"
  runtime          = "python3.12"
  filename         = (var.worker_s3_bucket == "" && var.worker_s3_key == "") ? var.worker_zip : null
  s3_bucket        = var.worker_s3_bucket != "" ? var.worker_s3_bucket : null
  s3_key           = var.worker_s3_key != "" ? var.worker_s3_key : null
  source_code_hash = (var.worker_s3_bucket == "" && var.worker_s3_key == "") ? filebase64sha256(var.worker_zip) : null
  timeout          = 300
  memory_size      = 512
  environment {
    variables = {
      SOURCE_LOCALE                     = var.source_locale
      TARGET_LOCALES                    = var.target_locales
      THEME_ID                          = var.theme_id
      THEME_RESOURCE_TYPES              = var.theme_resource_types
      THEME_POLL_ENABLED                = var.disable_sync == "true" ? "false" : var.scheduled_sync_enabled
      GLOBAL_RESOURCE_TYPES             = var.global_resource_types
      GLOBAL_RESOURCE_POLL_ENABLED      = var.global_resource_poll_enabled
      SHOP_DOMAIN                       = var.shop_domain
      SHOPIFY_API_VERSION               = var.shopify_api_version
      OPENAI_API_KEY_SECRET_ARN         = var.openai_api_key_secret_arn
      SHOPIFY_ADMIN_TOKEN_SECRET_ARN    = var.shopify_admin_token_secret_arn
      NEON_DATABASE_URL_SECRET_ARN      = var.neon_database_url_secret_arn
      REQUEST_TIMEOUT                   = var.request_timeout
      RETRIES                           = var.retries
      DRY_RUN                           = var.dry_run
      LOG_VERBOSE_SYNC                  = var.log_verbose_sync
      OPENAI_MODEL                      = var.openai_model
      OPENAI_FALLBACK_MODEL             = var.openai_fallback_model
      TRANSLATOR_SPECIALIZATION         = var.translator_specialization
      TRANSLATOR_BRAND                  = var.translator_brand
      TRANSLATOR_AUDIENCE               = var.translator_audience
      SOURCE_LANGUAGE_NAME              = var.source_language_name
      DO_NOT_TRANSLATE_YAML             = var.do_not_translate_yaml
      METAFIELD_TRANSLATION_POLICY_PATH = var.metafield_translation_policy_path
      THEME_TRANSLATION_POLICY_PATH     = var.theme_translation_policy_path
    }
  }
}

resource "aws_iam_user" "cloudflare_admin_invoker" {
  name = "${local.name_prefix}-cloudflare-admin-invoker"
  path = "/service-accounts/"
}

resource "aws_iam_user_policy" "cloudflare_admin_invoker" {
  name = "${local.name_prefix}-invoke-theme-poller"
  user = aws_iam_user.cloudflare_admin_invoker.name
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Effect   = "Allow",
        Action   = ["lambda:InvokeFunction"],
        Resource = aws_lambda_function.theme_poller.arn
      }
    ]
  })
}

resource "aws_iam_access_key" "cloudflare_admin_invoker" {
  user   = aws_iam_user.cloudflare_admin_invoker.name
  status = "Active"
}

resource "aws_cloudwatch_event_rule" "theme_poll" {
  count               = local.theme_poll_enabled ? 1 : 0
  name                = "${local.name_prefix}-theme-poll"
  schedule_expression = var.theme_poll_schedule
  state               = var.scheduled_sync_enabled == "true" ? "ENABLED" : "DISABLED"
}

resource "aws_cloudwatch_event_target" "theme_poll" {
  count = local.theme_poll_enabled ? 1 : 0
  rule  = aws_cloudwatch_event_rule.theme_poll[0].name
  arn   = aws_lambda_function.theme_poller.arn
}

resource "aws_lambda_permission" "theme_poll" {
  count         = local.theme_poll_enabled ? 1 : 0
  statement_id  = "AllowEventBridgeInvokeThemePoller"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.theme_poller.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.theme_poll[0].arn
}
