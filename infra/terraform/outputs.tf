output "receiver_function_url" {
  value = aws_lambda_function_url.receiver.function_url
}

output "sqs_queue_url" {
  value = aws_sqs_queue.products.id
}

output "dynamodb_tables" {
  value = {
    product_snapshots = aws_dynamodb_table.product_snapshots.name
    webhook_dedup     = aws_dynamodb_table.webhook_dedup.name
  }
}

output "cloudflare_admin_aws_access_key_id" {
  value     = aws_iam_access_key.cloudflare_admin_invoker.id
  sensitive = true
}

output "cloudflare_admin_aws_secret_access_key" {
  value     = aws_iam_access_key.cloudflare_admin_invoker.secret
  sensitive = true
}

output "theme_poller_function_name" {
  value = aws_lambda_function.theme_poller.function_name
}
