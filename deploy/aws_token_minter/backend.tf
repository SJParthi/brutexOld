# Remote Terraform state backend for the centralized Groww token-minter stack
# (DEC-DEPLOY-REMOTE-STATE-001, DEC-TOKEN-MINTER-CENTRAL-001).
#
# WHY REMOTE STATE.
#   With LOCAL state (terraform.tfstate on disk, gitignored) a FRESH CLONE has NO
#   state, so `terraform apply` from a new machine tries to RE-CREATE resources
#   that already exist in AWS -> "EntityAlreadyExists" / "ResourceInUse" errors.
#   Storing state in AWS (an S3 bucket + a DynamoDB lock table) makes the deploy
#   FRESH-CLONE-REPEATABLE + IDEMPOTENT: any clone on any machine reads the SAME
#   remote state and `apply` converges to a no-op once the stack exists.
#
# CHICKEN-AND-EGG (bootstrap).
#   The S3 bucket + DynamoDB table must EXIST before Terraform can use them as a
#   backend. deploy.sh IDEMPOTENTLY creates them via the AWS CLI (check-then-create)
#   BEFORE `terraform init`, then runs `terraform init -backend-config=...` pointing
#   at the concrete bucket/table/region. This block is intentionally PARTIAL (only
#   the fixed key + region are baked here); the bucket + lock table names are
#   account-derived (tickvault-tfstate-<ACCOUNT_ID> / tickvault-tfstate-lock) and
#   supplied by deploy.sh via `-backend-config` so the .tf never hardcodes an
#   account id.
#
# ONE-TIME MIGRATION (existing local state).
#   An operator who already deployed with LOCAL state runs, ONCE:
#       terraform init -migrate-state -backend-config=...   (answer "yes")
#   which copies the on-disk terraform.tfstate into S3. deploy.sh does a plain
#   `terraform init` (no -migrate-state) on every run; the migration is a
#   deliberate one-time operator step so we never silently move state.

terraform {
  backend "s3" {
    # region + key are fixed here; bucket + dynamodb_table are injected by
    # deploy.sh via -backend-config (they are account-id-derived).
    key     = "tickvault/groww-token-minter/terraform.tfstate"
    region  = "ap-south-1"
    encrypt = true
  }
}
