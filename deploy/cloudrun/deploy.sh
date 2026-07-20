#!/usr/bin/env bash
set -euo pipefail

: "${PROJECT_ID:?Set PROJECT_ID}"
: "${DATABASE_URL:?Set the Supabase pooled PostgreSQL DATABASE_URL}"

if [[ -n "${STRIPE_SECRET_KEY:-}" || -n "${STRIPE_WEBHOOK_SECRET:-}" ]]; then
  : "${STRIPE_SECRET_KEY:?Set both Stripe secrets or neither}"
  : "${STRIPE_WEBHOOK_SECRET:?Set both Stripe secrets or neither}"
fi
if [[ -n "${SOUTUI_BOOTSTRAP_MERCHANT_EMAIL:-}" || -n "${SOUTUI_BOOTSTRAP_MERCHANT_PASSWORD:-}" ]]; then
  : "${SOUTUI_BOOTSTRAP_MERCHANT_EMAIL:?Set both merchant bootstrap values or neither}"
  : "${SOUTUI_BOOTSTRAP_MERCHANT_PASSWORD:?Set both merchant bootstrap values or neither}"
fi

REGION="${REGION:-us-east1}"
SERVICE_NAME="${SERVICE_NAME:-soutui-api}"
JOB_NAME="${JOB_NAME:-soutui-train}"
REPOSITORY="${REPOSITORY:-soutui}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/soutui:latest"

gcloud config set project "${PROJECT_ID}"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com

if ! gcloud artifacts repositories describe "${REPOSITORY}" --location "${REGION}" >/dev/null 2>&1; then
  gcloud artifacts repositories create "${REPOSITORY}" --repository-format docker --location "${REGION}"
fi

gcloud builds submit --tag "${IMAGE}" .

upsert_secret() {
  local name="$1" value="$2"
  if ! gcloud secrets describe "${name}" >/dev/null 2>&1; then
    printf '%s' "${value}" | gcloud secrets create "${name}" --replication-policy automatic --data-file=-
  else
    printf '%s' "${value}" | gcloud secrets versions add "${name}" --data-file=-
  fi
}

upsert_secret soutui-database-url "${DATABASE_URL}"

SECRETS=(soutui-database-url)
SECRET_MAPPINGS=(DATABASE_URL=soutui-database-url:latest)
if [[ -n "${STRIPE_SECRET_KEY:-}" ]]; then
  upsert_secret soutui-stripe-key "${STRIPE_SECRET_KEY}"
  upsert_secret soutui-stripe-webhook-secret "${STRIPE_WEBHOOK_SECRET}"
  SECRETS+=(soutui-stripe-key soutui-stripe-webhook-secret)
  SECRET_MAPPINGS+=(STRIPE_SECRET_KEY=soutui-stripe-key:latest STRIPE_WEBHOOK_SECRET=soutui-stripe-webhook-secret:latest)
fi
if [[ -n "${SOUTUI_BOOTSTRAP_MERCHANT_EMAIL:-}" ]]; then
  upsert_secret soutui-merchant-email "${SOUTUI_BOOTSTRAP_MERCHANT_EMAIL}"
  upsert_secret soutui-merchant-password "${SOUTUI_BOOTSTRAP_MERCHANT_PASSWORD}"
  SECRETS+=(soutui-merchant-email soutui-merchant-password)
  SECRET_MAPPINGS+=(SOUTUI_BOOTSTRAP_MERCHANT_EMAIL=soutui-merchant-email:latest SOUTUI_BOOTSTRAP_MERCHANT_PASSWORD=soutui-merchant-password:latest)
fi

RUNTIME_ACCOUNT="soutui-runtime@${PROJECT_ID}.iam.gserviceaccount.com"
if ! gcloud iam service-accounts describe "${RUNTIME_ACCOUNT}" >/dev/null 2>&1; then
  gcloud iam service-accounts create soutui-runtime --display-name='Soutui Cloud Run runtime'
fi
for secret in "${SECRETS[@]}"; do
  gcloud secrets add-iam-policy-binding "${secret}" \
    --member="serviceAccount:${RUNTIME_ACCOUNT}" --role='roles/secretmanager.secretAccessor' >/dev/null
done

SECRET_ARG="$(IFS=,; echo "${SECRET_MAPPINGS[*]}")"

gcloud run deploy "${SERVICE_NAME}" \
  --image "${IMAGE}" --region "${REGION}" --allow-unauthenticated \
  --service-account "${RUNTIME_ACCOUNT}" \
  --port 8080 --cpu 1 --memory 1Gi --min-instances 0 --max-instances 5 \
  --set-env-vars 'SOUTUI_SECURE_COOKIE=1,DB_POOL_SIZE=5,SOUTUI_MODEL_REFRESH_SECONDS=60' \
  --set-secrets "${SECRET_ARG}"

gcloud run jobs deploy "${JOB_NAME}" \
  --image "${IMAGE}" --region "${REGION}" \
  --service-account "${RUNTIME_ACCOUNT}" \
  --command python --args=-m,soutui.training \
  --cpu 1 --memory 1Gi --max-retries 1 --task-timeout 30m \
  --set-env-vars 'DB_POOL_SIZE=2' \
  --set-secrets 'DATABASE_URL=soutui-database-url:latest'

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" --region "${REGION}" --format='value(status.url)')"
echo "Service: ${SERVICE_URL}"
echo "Stripe webhook: ${SERVICE_URL}/webhooks/stripe"
echo "Run training: gcloud run jobs execute ${JOB_NAME} --region ${REGION} --wait"
