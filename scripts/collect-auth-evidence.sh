#!/usr/bin/env bash
# Collects the evidence for the connector-login problem, for both services.
# Read-only: changes nothing. Prints no tokens or secrets (FastMCP logs only
# 8-character token-hash prefixes; URL query strings are cut off).
#
# Usage (Cloud Shell, as a project owner):
#   bash collect-auth-evidence.sh [START_UTC] [END_UTC] > auth-evidence.txt
set -uo pipefail
export CLOUDSDK_CORE_DISABLE_PROMPTS=1
PROJECT_ID="djavaweb-ads-mcp"
REGION="us-central1"
START="${1:-2026-10-23T01:15:00Z}"
END="${2:-2026-10-23T02:40:00Z}"
gcloud config set project "$PROJECT_ID" >/dev/null 2>&1

section() { printf '\n===== %s =====\n' "$*"; }

for SVC in google-ads-mcp google-ads-actions; do
  section "$SVC: config"
  gcloud run services describe "$SVC" --region "$REGION" --format=json | python3 -c '
import json, sys
d = json.load(sys.stdin); t = d["spec"]["template"]; c = t["spec"]["containers"][0]
print("image:", c["image"]); print("latest revision:", d["status"].get("latestReadyRevisionName"))
print("scaling:", {k.split("/")[-1]: v for k, v in t["metadata"].get("annotations", {}).items() if "Scale" in k})
for e in c.get("env", []):
    if "valueFrom" in e: print("  env", e["name"], "<- secret", e["valueFrom"]["secretKeyRef"]["name"])
    elif "SECRET" in e["name"] or "KEY" in e["name"] or "TOKEN" in e["name"]: print("  env", e["name"], "= <set>")
    else: print("  env", e["name"], "=", e["value"])'

  section "$SVC: revisions"
  gcloud run revisions list --service "$SVC" --region "$REGION" --format='table(metadata.name,metadata.creationTimestamp)' | head -15

  section "$SVC: OAuth and /mcp requests $START .. $END (OAuth endpoints; /mcp if non-2xx or >= 30 s)"
  gcloud logging read "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"$SVC\" AND logName=\"projects/$PROJECT_ID/logs/run.googleapis.com%2Frequests\" AND timestamp>=\"$START\" AND timestamp<=\"$END\"" \
    --order=asc --limit=2000 --format=json | python3 -c '
import json, sys
from urllib.parse import urlsplit
for e in json.load(sys.stdin):
    r = e.get("httpRequest", {}); path = urlsplit(r.get("requestUrl", "")).path; st = r.get("status", 0)
    oauth = any(path.startswith(p) for p in ("/token", "/authorize", "/auth/callback", "/register", "/revoke", "/.well-known", "/consent"))
    lat = float(str(r.get("latency", "0s")).rstrip("s") or 0)
    if oauth or (path.startswith("/mcp") and (not 200 <= st < 300 or lat >= 30)):
        print(e["timestamp"][:23], st, r.get("requestMethod", ""), path, r.get("latency", ""), "inst=" + e.get("labels", {}).get("instanceId", "")[-8:])'

  section "$SVC: auth log lines $START .. $END"
  gcloud logging read "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"$SVC\" AND timestamp>=\"$START\" AND timestamp<=\"$END\" AND (textPayload:\"efresh\" OR textPayload:\"JTI\" OR textPayload:\"nvalid\" OR textPayload:\"Upstream\" OR textPayload:\"Auth error\" OR textPayload:\"token\" OR textPayload:\"oauth_hardening\" OR textPayload:\"Uvicorn running\" OR textPayload:\"Traceback\" OR severity>=WARNING)" \
    --order=asc --limit=2000 --format='value(timestamp,severity,textPayload)' | cut -c1-400

  section "$SVC: instance starts $START .. $END"
  gcloud logging read "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"$SVC\" AND timestamp>=\"$START\" AND timestamp<=\"$END\" AND (textPayload:\"Starting new instance\" OR textPayload:\"Uvicorn running\" OR textPayload:\"Application startup complete\")" \
    --order=asc --limit=200 --format='value(timestamp,textPayload)' | cut -c1-200
done

section "Firestore collections (default database)"
curl -s -X POST -H "Authorization: Bearer $(gcloud auth print-access-token)" -H "Content-Type: application/json" -d '{"pageSize": 300}' \
  "https://firestore.googleapis.com/v1/projects/$PROJECT_ID/databases/(default)/documents:listCollectionIds" | python3 -c 'import json,sys; print("\n".join(json.load(sys.stdin).get("collectionIds", [])))'

section "done"
