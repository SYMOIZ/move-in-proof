#!/usr/bin/env bash
# Deploys Move-In Proof with the plain AWS CLI (no SAM CLI, no pipeline).
#   ./deploy.sh                 demo settings: landlord has 120 s to co-sign, 1-day lock
#   COSIGN_WINDOW=259200 RETENTION_DAYS=365 ./deploy.sh
set -euo pipefail
cd "$(dirname "$0")"

STACK="${STACK:-move-in-proof}"
REGION="${AWS_REGION:-$(aws configure get region)}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
ARTIFACTS="${ARTIFACTS:-move-in-proof-artifacts-$ACCOUNT-$REGION}"

aws s3api head-bucket --bucket "$ARTIFACTS" 2>/dev/null || aws s3 mb "s3://$ARTIFACTS" --region "$REGION"

aws cloudformation package --template-file template.yaml --s3-bucket "$ARTIFACTS" \
  --output-template-file packaged.yaml --region "$REGION"

aws cloudformation deploy --template-file packaged.yaml --stack-name "$STACK" --region "$REGION" \
  --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND \
  --parameter-overrides "CoSignWindowSeconds=${COSIGN_WINDOW:-120}" "RetentionDays=${RETENTION_DAYS:-1}"

aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='AppUrl'].OutputValue" --output text
