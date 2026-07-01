# Outputs for the centralized Groww token-minter stack
# (DEC-TOKEN-MINTER-CENTRAL-001).

output "token_param_name" {
  description = "SSM path of the shared Groww access token (readers point here, READ-ONLY)."
  value       = aws_ssm_parameter.access_token.name
}

output "token_param_arn" {
  description = "ARN of the shared token param — the resource the reader roles are scoped to."
  value       = aws_ssm_parameter.access_token.arn
}

output "minter_lambda_name" {
  description = "Name of the minter Lambda (invoke on-demand with `aws lambda invoke`)."
  value       = aws_lambda_function.minter.function_name
}

output "minter_lambda_arn" {
  description = "ARN of the minter Lambda."
  value       = aws_lambda_function.minter.arn
}

output "minter_role_arn" {
  description = "ARN of the least-privilege minter IAM role (reads creds, writes token)."
  value       = aws_iam_role.minter.arn
}

output "reader_tickvault_role_arn" {
  description = "ARN of the TickVault reader role (GetParameter + KMS Decrypt on the token only)."
  value       = aws_iam_role.reader_tickvault.arn
}

output "reader_brutex_role_arn" {
  description = "ARN of the BruteX reader role (GetParameter + KMS Decrypt on the token only)."
  value       = aws_iam_role.reader_brutex.arn
}

output "schedule_rule_arn" {
  description = "ARN of the daily EventBridge schedule rule."
  value       = aws_cloudwatch_event_rule.daily_mint.arn
}

output "alerts_topic_arn" {
  description = "ARN of the SNS topic the stale-token + error alarms publish to."
  value       = aws_sns_topic.alerts.arn
}

output "stale_token_alarm_arn" {
  description = "ARN of the stale-token / failed-mint CloudWatch alarm."
  value       = aws_cloudwatch_metric_alarm.stale_token.arn
}
