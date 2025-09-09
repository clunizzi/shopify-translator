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

