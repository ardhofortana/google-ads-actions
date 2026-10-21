#!/usr/bin/env bash
# Deploys the google-ads-actions MCP server to Cloud Run.
#
# Same project, region, OAuth client, client secret and Firestore as the
# read-only google-ads-mcp service; its own JWT signing key. The image is
# tagged with the git commit so the deployed revision maps to a commit.
#
# Usage: ./deploy-google-ads-actions.sh   (run from the repo root, gcloud logged in
#        with rights to Cloud Run, Cloud Build, Artifact Registry, Secret Manager and IAM)
set -euo pipefail
export CLOUDSDK_CORE_DISABLE_PROMPTS=1

PROJECT_ID="djavaweb-ads-mcp"
PROJECT_NUMBER="602690127469"
REGION="us-central1"
SERVICE="google-ads-actions"
BASE_URL="https://${SERVICE}-${PROJECT_NUMBER}.${REGION}.run.app"
OAUTH_CLIENT_ID="602690127469-icrhln7u9os9svj5b914m8fmmu5icmng.apps.googleusercontent.com"
CLIENT_SECRET_NAME="gads-mcp-client-secret"
JWT_SECRET_NAME="gads-actions-jwt-key"
# Optional. The read-only service runs without a developer token (API access comes
# from the Cloud project). Set this to a Secret Manager secret name to pass one.
DEV_TOKEN_SECRET_NAME="${DEV_TOKEN_SECRET_NAME:-}"
RUNTIME_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
REPOSITORY="mcp-servers"

if [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
  echo "Refusing to deploy: uncommitted changes. Commit first so the deployed image maps to a commit." >&2
  exit 1
fi
COMMIT_SHA="$(git rev-parse --short=12 HEAD)"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/${SERVICE}:${COMMIT_SHA}"

gcloud config set project "$PROJECT_ID" >/dev/null

echo "==> Enabling APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com firestore.googleapis.com googleads.googleapis.com

echo "==> Checking secrets"
SECRETS=("$CLIENT_SECRET_NAME" "$JWT_SECRET_NAME")
if ! gcloud secrets describe "$CLIENT_SECRET_NAME" >/dev/null 2>&1; then
  echo "Missing secret '$CLIENT_SECRET_NAME'." >&2; exit 1
fi
if ! gcloud secrets describe "$JWT_SECRET_NAME" >/dev/null 2>&1; then
  echo "    creating $JWT_SECRET_NAME"
  openssl rand -base64 48 | tr -d '\n' | gcloud secrets create "$JWT_SECRET_NAME" --replication-policy=automatic --data-file=-
fi
if [[ -n "$DEV_TOKEN_SECRET_NAME" ]]; then
  gcloud secrets describe "$DEV_TOKEN_SECRET_NAME" >/dev/null || { echo "Missing secret '$DEV_TOKEN_SECRET_NAME'." >&2; exit 1; }
  SECRETS+=("$DEV_TOKEN_SECRET_NAME")
fi

retry_iam() {
  # IAM bindings race with other policy edits; retry on "concurrent policy changes".
  local attempt out
  for attempt in 1 2 3 4 5 6; do
    if out="$("$@" 2>&1)"; then return 0; fi
    if grep -qi "concurrent policy changes" <<<"$out"; then
      echo "    concurrent policy change, retrying ($attempt)"; sleep $((attempt * 3)); continue
    fi
    echo "$out" >&2; return 1
  done
  echo "$out" >&2; return 1
}

echo "==> Granting runtime service account access"
for s in "${SECRETS[@]}"; do
  retry_iam gcloud secrets add-iam-policy-binding "$s" \
    --member="serviceAccount:${RUNTIME_SA}" --role="roles/secretmanager.secretAccessor" --condition=None
done
retry_iam gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${RUNTIME_SA}" --role="roles/datastore.user" --condition=None

echo "==> Building $IMAGE"
gcloud artifacts repositories describe "$REPOSITORY" --location "$REGION" >/dev/null 2>&1 || \
  gcloud artifacts repositories create "$REPOSITORY" --location "$REGION" --repository-format docker
# --async + polling: streaming build logs needs project Viewer, which a deploy-only account may lack.
BUILD_ID="$(gcloud builds submit --region "$REGION" --tag "$IMAGE" --async --format='value(id)' .)"
echo "    build $BUILD_ID"
while true; do
  STATUS="$(gcloud builds describe "$BUILD_ID" --region "$REGION" --format='value(status)')"
  case "$STATUS" in
    SUCCESS) break ;;
    QUEUED|PENDING|WORKING) sleep 10 ;;
    *) echo "Build $BUILD_ID ended with $STATUS" >&2
       gcloud builds describe "$BUILD_ID" --region "$REGION" --format='value(failureInfo.detail,logUrl)' >&2 || true
       exit 1 ;;
  esac
done

echo "==> Deploying $SERVICE"
SET_SECRETS="GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET=${CLIENT_SECRET_NAME}:latest,GOOGLE_ADS_MCP_JWT_SIGNING_KEY=${JWT_SECRET_NAME}:latest"
[[ -n "$DEV_TOKEN_SECRET_NAME" ]] && SET_SECRETS+=",GOOGLE_ADS_DEVELOPER_TOKEN=${DEV_TOKEN_SECRET_NAME}:latest"
gcloud run deploy "$SERVICE" \
  --image "$IMAGE" \
  --region "$REGION" \
  --service-account "$RUNTIME_SA" \
  --allow-unauthenticated \
  --min-instances 0 \
  --max-instances 2 \
  --memory 512Mi \
  --labels "commit-sha=${COMMIT_SHA}" \
  --set-env-vars "GOOGLE_ADS_MCP_OAUTH_CLIENT_ID=${OAUTH_CLIENT_ID},GOOGLE_ADS_MCP_BASE_URL=${BASE_URL},GOOGLE_ADS_MCP_STORAGE_TYPE=firestore,GOOGLE_ADS_MCP_STORAGE_FIRESTORE_PROJECT=${PROJECT_ID}" \
  --set-secrets "$SET_SECRETS"

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
REVISION="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.latestReadyRevisionName)')"

echo "==> Smoke check"
code="$(curl -s -o /dev/null -w '%{http_code}' -X POST "${BASE_URL}/mcp" -H 'content-type: application/json' -d '{}')"
[[ "$code" == "401" ]] && echo "    /mcp without a token -> 401 (OAuth required) OK" || { echo "    expected 401 from /mcp, got $code" >&2; exit 1; }
curl -sf "${BASE_URL}/.well-known/oauth-authorization-server" >/dev/null && echo "    OAuth metadata OK"

cat <<MSG

Deployed.
  URL:      ${URL}  (MCP endpoint ${BASE_URL}/mcp)
  Revision: ${REVISION}
  Image:    ${IMAGE}
  Commit:   ${COMMIT_SHA}

Before connecting (one-time, Google Cloud console > APIs & Services > Credentials >
OAuth client "google-ads-mcp"): ADD this authorized redirect URI, keep the existing one:
  ${BASE_URL}/auth/callback

Then in Claude: Settings > Connectors > Add custom connector
  Name: Google Ads Actions    URL: ${BASE_URL}/mcp    (leave advanced client ID/secret empty)
  Sign in as ardhofortana@gmail.com and tick every permission box on Google's consent screen.
MSG
