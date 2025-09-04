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
  name_prefix = "${var.project}-shopify"
}

resource "aws_sqs_queue" "products" {
  name                       = "${local.name_prefix}-products-queue"
  visibility_timeout_seconds = 60
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
  function_name = "${local.name_prefix}-webhook-receiver"
  role          = aws_iam_role.receiver.arn
  handler       = "src/aws_lambda/receiver.handler"
  runtime       = "python3.12"
  filename      = var.receiver_zip
  source_code_hash = filebase64sha256(var.receiver_zip)
  environment {
    variables = {
      SQS_URL                    = aws_sqs_queue.products.id
      SHOPIFY_WEBHOOK_SECRET_ARN = var.shopify_webhook_secret_arn
      # Optional fallback for testing only (avoid using in prod):
      # SHOPIFY_WEBHOOK_SECRET   = var.shopify_webhook_secret
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
        Effect   = "Allow",
        Action   = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:ChangeMessageVisibility",
          "sqs:GetQueueAttributes"
        ],
        Resource = aws_sqs_queue.products.arn
      },
      {
        Effect   = "Allow",
        Action   = ["secretsmanager:GetSecretValue"],
        Resource = compact([
          var.openai_api_key_secret_arn,
          var.shopify_admin_token_secret_arn
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
  filename         = (var.worker_s3_bucket == "" && var.worker_s3_key == "") ? var.worker_zip : null
  s3_bucket        = var.worker_s3_bucket != "" ? var.worker_s3_bucket : null
  s3_key           = var.worker_s3_key    != "" ? var.worker_s3_key    : null
  # Only compute hash when using local file
  source_code_hash = (var.worker_s3_bucket == "" && var.worker_s3_key == "") ? filebase64sha256(var.worker_zip) : null
  timeout       = 60
  environment {
    variables = {
      DDB_TABLE        = aws_dynamodb_table.product_snapshots.name
      DEDUP_TABLE      = aws_dynamodb_table.webhook_dedup.name
      SOURCE_LOCALE    = var.source_locale
      TARGET_LOCALES   = var.target_locales
      MF_INCLUDE       = var.mf_include
      MF_JSON_PATHS    = var.mf_json_paths
      SHOP_DOMAIN      = var.shop_domain
      # Secrets Manager ARNs for runtime retrieval
      OPENAI_API_KEY_SECRET_ARN      = var.openai_api_key_secret_arn
      SHOPIFY_ADMIN_TOKEN_SECRET_ARN = var.shopify_admin_token_secret_arn
      REQUEST_TIMEOUT  = var.request_timeout
      RETRIES          = var.retries
      DEBOUNCE_SECONDS = var.debounce_seconds
      DRY_RUN          = var.dry_run
    }
  }
}

resource "aws_lambda_event_source_mapping" "worker_sqs" {
  event_source_arn = aws_sqs_queue.products.arn
  function_name    = aws_lambda_function.worker.arn
  batch_size       = 5
}
