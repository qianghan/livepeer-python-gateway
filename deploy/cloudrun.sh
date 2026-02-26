#!/usr/bin/env bash
# Deploy livepeer-gateway to Google Cloud Run.
#
# Usage:
#   export GCP_PROJECT=my-project
#   export GCP_REGION=us-central1        # optional, default us-central1
#   export SERVICE_NAME=livepeer-gateway  # optional
#   ./deploy/cloudrun.sh
#
# Prerequisites:
#   - gcloud CLI authenticated (gcloud auth login)
#   - Docker authenticated to GCR (gcloud auth configure-docker)
#   - API_KEYS stored in GCP Secret Manager as "livepeer-api-keys"
#   - LIVEPEER_TOKEN stored in GCP Secret Manager as "livepeer-token"

set -euo pipefail

PROJECT="${GCP_PROJECT:?Set GCP_PROJECT env var}"
REGION="${GCP_REGION:-us-central1}"
SERVICE="${SERVICE_NAME:-livepeer-gateway}"
IMAGE="gcr.io/${PROJECT}/${SERVICE}"

echo "==> Building image..."
docker build -t "${IMAGE}" .

echo "==> Pushing to GCR..."
docker push "${IMAGE}"

echo "==> Deploying to Cloud Run (${REGION})..."
gcloud run deploy "${SERVICE}" \
    --project "${PROJECT}" \
    --region "${REGION}" \
    --image "${IMAGE}" \
    --platform managed \
    --allow-unauthenticated \
    --port 8000 \
    --timeout 3600 \
    --session-affinity \
    --concurrency 10 \
    --min-instances 0 \
    --max-instances 10 \
    --memory 1Gi \
    --cpu 1 \
    --set-env-vars "DEFAULT_MODEL_ID=${DEFAULT_MODEL_ID:-noop},FPS=${FPS:-24},JPEG_QUALITY=${JPEG_QUALITY:-80},MAX_JOBS_PER_KEY=${MAX_JOBS_PER_KEY:-10}" \
    --update-secrets "API_KEYS=livepeer-api-keys:latest,LIVEPEER_TOKEN=livepeer-token:latest" \
    ${DAYDREAM_URL:+--set-env-vars "DAYDREAM_URL=${DAYDREAM_URL}"} \
    ${ORCHESTRATOR_URL:+--set-env-vars "ORCHESTRATOR_URL=${ORCHESTRATOR_URL}"}

echo "==> Deployment complete."
gcloud run services describe "${SERVICE}" \
    --project "${PROJECT}" \
    --region "${REGION}" \
    --format "value(status.url)"
