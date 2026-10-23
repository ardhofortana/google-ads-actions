#!/usr/bin/env bash
# Rebuilds the read-only google-ads-mcp service on its CURRENT image with the
# refresh-token reuse window added, and deploys it. Env vars, secrets, OAuth
# client and Firestore collections are untouched, so existing logins stay valid.
#
# Rollback: gcloud run services update-traffic google-ads-mcp --region us-central1 \
#             --to-revisions <previous revision>=100
set -euo pipefail
export CLOUDSDK_CORE_DISABLE_PROMPTS=1

PROJECT_ID="djavaweb-ads-mcp"
REGION="us-central1"
SERVICE="google-ads-mcp"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMMIT_SHA="$(git -C "$ROOT" rev-parse --short=12 HEAD)"
gcloud config set project "$PROJECT_ID" >/dev/null

CURRENT_IMAGE="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(spec.template.spec.containers[0].image)')"
PREVIOUS_REVISION="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.latestReadyRevisionName)')"
case "$CURRENT_IMAGE" in
  *@sha256:*) BASE="$CURRENT_IMAGE" ;;
  *) BASE="${CURRENT_IMAGE%:*}@$(gcloud artifacts docker images describe "$CURRENT_IMAGE" --format='value(image_summary.digest)')" ;;
esac
# Never stack the patch on an already-patched image.
case "$CURRENT_IMAGE" in *authfix*) echo "Service already runs a patched image ($CURRENT_IMAGE). Rebuild from the original image manually." >&2; exit 1 ;; esac
NEW_IMAGE="${CURRENT_IMAGE%%[:@]*}:authfix-${COMMIT_SHA}"
echo "==> Base:  $BASE"
echo "==> New:   $NEW_IMAGE"

CTX="$(mktemp -d)"
cp "$ROOT/ads_actions/oauth_hardening.py" "$ROOT/readonly-patch/sitecustomize.py" "$CTX/"
sed "s|^FROM BASE_IMAGE|FROM ${BASE}|" "$ROOT/readonly-patch/Dockerfile" > "$CTX/Dockerfile"

BUILD_ID="$(gcloud builds submit "$CTX" --region "$REGION" --tag "$NEW_IMAGE" --async --format='value(id)')"
echo "    build $BUILD_ID"
while true; do
  STATUS="$(gcloud builds describe "$BUILD_ID" --region "$REGION" --format='value(status)')"
  case "$STATUS" in
    SUCCESS) break ;;
    QUEUED|PENDING|WORKING) sleep 10 ;;
    *) echo "Build $BUILD_ID ended with $STATUS" >&2; exit 1 ;;
  esac
done

echo "==> Deploying $SERVICE (previous revision: $PREVIOUS_REVISION)"
gcloud run deploy "$SERVICE" --image "$NEW_IMAGE" --region "$REGION" --update-labels "authfix-commit=${COMMIT_SHA}"
echo "Done. Rollback: gcloud run services update-traffic $SERVICE --region $REGION --to-revisions ${PREVIOUS_REVISION}=100"
