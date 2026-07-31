#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TFVARS="${TFVARS:-$ROOT_DIR/infra/terraform/terraform.tfvars}"
PY_VERSION="${PY:-3.12}"
PLAN_FILE="${PLAN_FILE:-worker-deploy.tfplan}"

read_tfvar() {
  local name="$1"
  sed -nE "s/^[[:space:]]*${name}[[:space:]]*=[[:space:]]*\"([^\"]*)\".*/\1/p" "$TFVARS" | tail -n 1
}

AWS_REGION="${AWS_REGION:-$(read_tfvar aws_region)}"
S3_BUCKET="${WORKER_S3_BUCKET:-$(read_tfvar worker_s3_bucket)}"

if [[ -z "${AWS_REGION}" ]]; then
  echo "Missing aws_region in $TFVARS or AWS_REGION env" >&2
  exit 1
fi

if [[ -z "${S3_BUCKET}" ]]; then
  echo "Missing worker_s3_bucket in $TFVARS or WORKER_S3_BUCKET env" >&2
  exit 1
fi

STAMP="$(date +%Y%m%dT%H%M%S)"
S3_KEY="${WORKER_S3_KEY:-worker/worker-${STAMP}.zip}"

cd "$ROOT_DIR"
make build-worker-docker "PY=${PY_VERSION}"
aws s3 cp build/worker.zip "s3://${S3_BUCKET}/${S3_KEY}" --region "$AWS_REGION"

if grep -qE '^[[:space:]]*worker_s3_key[[:space:]]*=' "$TFVARS"; then
  sed -i -E "s|^[[:space:]]*worker_s3_key[[:space:]]*=.*|worker_s3_key      = \"${S3_KEY}\"|" "$TFVARS"
else
  printf '\nworker_s3_key      = "%s"\n' "$S3_KEY" >> "$TFVARS"
fi

terraform -chdir=infra/terraform init
terraform -chdir=infra/terraform plan -out="$PLAN_FILE"

echo "Built and uploaded worker_s3_key=${S3_KEY}"
echo "Review the Terraform plan above."

if [[ "${DEPLOY_APPLY:-0}" == "1" ]]; then
  terraform -chdir=infra/terraform apply "$PLAN_FILE"
  echo "Applied ${PLAN_FILE}"
else
  echo "No infrastructure changes applied. Re-run with DEPLOY_APPLY=1 after review."
fi
