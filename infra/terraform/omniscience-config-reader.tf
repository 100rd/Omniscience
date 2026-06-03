# Terraform stub — Omniscience Config-aggregator read-only role (ADR-0014 Phase 1)
#
# HUMAN-GATED STEP: Applying this stub is out of scope for the code PR.
# It belongs in the qbiq-ai/infra repository under the Log Archive account module.
# Apply steps:
#   1. Copy this file to qbiq-ai/infra/<env>/log-archive/ (or the equivalent module).
#   2. Replace all REPLACE_WITH_* placeholders with real values from your environment.
#   3. Raise an infra PR, get approval, then: terragrunt apply
#   4. Record the output role ARN and provide it as the secrets_ref for the AWS source.
#
# This stub intentionally uses no data sources or remote state references so it
# can be reviewed without access to the infra repo.

# --------------------------------------------------------------------------
# Variables — wire to your terragrunt inputs or .tfvars
# --------------------------------------------------------------------------

variable "omniscience_principal_account_id" {
  type        = string
  description = "AWS account ID where Omniscience runs (needs to assume this role)."
}

variable "omniscience_external_id" {
  type        = string
  description = "STS external-id secret for the trust policy (prevents confused deputy)."
}

variable "config_delivery_bucket_name" {
  type        = string
  description = "Name of the S3 bucket where Config delivers snapshots/history."
}

variable "log_archive_account_id" {
  type        = string
  description = "AWS account ID of the Log Archive account (where the aggregator lives)."
}

# --------------------------------------------------------------------------
# IAM Policy — read-only access to Config aggregator + S3 cold tier
# --------------------------------------------------------------------------

resource "aws_iam_policy" "omniscience_config_reader" {
  name        = "omniscience-config-reader"
  description = "Omniscience read-only access to the org Config aggregator and Config-S3 cold tier."
  path        = "/omniscience/"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "OmniscienceConfigAggregatorRead"
        Effect = "Allow"
        Action = [
          "config:SelectAggregateResourceConfig",
          "config:BatchGetAggregateResourceConfig",
          "config:ListAggregateDiscoveredResources",
          "config:GetAggregateResourceConfig",
          "config:GetAggregateDiscoveredResourceCounts",
          "config:GetAggregateResourceConfigHistory",
          "config:DescribeConfigurationAggregators",
        ]
        Resource = "*"
      },
      {
        Sid    = "OmniscienceConfigS3ColdRead"
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = "arn:aws:s3:::${var.config_delivery_bucket_name}/config/*"
      },
      {
        Sid    = "OmniscienceConfigS3ListBucket"
        Effect = "Allow"
        Action = ["s3:ListBucket"]
        Resource = "arn:aws:s3:::${var.config_delivery_bucket_name}"
      },
    ]
  })

  tags = {
    ManagedBy   = "terraform"
    Application = "omniscience"
    Phase       = "adr-0014-phase1"
  }
}

# --------------------------------------------------------------------------
# IAM Role — assumed by Omniscience via STS AssumeRole
# --------------------------------------------------------------------------

resource "aws_iam_role" "omniscience_config_reader" {
  name        = "omniscience-config-reader"
  description = "Role assumed by Omniscience to read the org Config aggregator (read-only)."
  path        = "/omniscience/"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "OmniscienceAssumeRole"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${var.omniscience_principal_account_id}:root"
        }
        Action = "sts:AssumeRole"
        Condition = {
          StringEquals = {
            "sts:ExternalId" = var.omniscience_external_id
          }
        }
      },
    ]
  })

  tags = {
    ManagedBy   = "terraform"
    Application = "omniscience"
    Phase       = "adr-0014-phase1"
  }
}

resource "aws_iam_role_policy_attachment" "omniscience_config_reader" {
  role       = aws_iam_role.omniscience_config_reader.name
  policy_arn = aws_iam_policy.omniscience_config_reader.arn
}

# --------------------------------------------------------------------------
# Outputs — feed these into the Omniscience secrets_ref
# --------------------------------------------------------------------------

output "omniscience_config_reader_role_arn" {
  description = "ARN of the role to provide as aws_role_arn in the Omniscience AWS source secrets."
  value       = aws_iam_role.omniscience_config_reader.arn
}
