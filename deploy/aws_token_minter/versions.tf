# Terraform + provider version constraints for the centralized Groww
# token-minter stack (DEC-TOKEN-MINTER-CENTRAL-001).
#
# The AWS provider is pinned to the 5.x major line; the resources used
# (aws_lambda_function, aws_cloudwatch_event_rule/target,
# aws_cloudwatch_metric_alarm, aws_sns_topic, aws_iam_role/policy,
# aws_ssm_parameter, the aws_ssm_parameter / aws_kms_key data sources) are all
# GA in that line. The archive provider zips the Lambda bundle; the null provider
# runs the local-exec build step (build_lambda_package.sh) that pip-installs
# growwapi + pyotp into the bundle. Bump deliberately, never floating.

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2"
    }
  }
}
