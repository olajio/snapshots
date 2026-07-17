#!/usr/bin/env bash
#
# cleanup_orphaned_searchable_snapshots.sh
#
# Finds and (optionally) deletes searchable snapshots in the `found-snapshots`
# repository that are NO LONGER referenced by any mounted searchable-snapshot
# index -- i.e. the orphans left behind when a frozen index was deleted (or its
# ILM policy edited) without ILM running the delete phase.
#
# It is DRY-RUN by default: it prints what it would delete and changes nothing.
# Pass --apply to actually delete.
#
# ---------------------------------------------------------------------------
# Requirements: bash, curl, jq  (plus the aws CLI when using --cluster/--secret-name)
#
# Authentication is API key only (no basic auth).
#
# Recommended -- fetch credentials from AWS Secrets Manager with --cluster. The
# secret is named `elastic/kibana/dataview_cleanup_<cluster>` and must contain:
#     es_url      -> the Elasticsearch endpoint
#     es_api_key  -> the API key ("encoded" value)
#       dev  -> elastic/kibana/dataview_cleanup_dev
#       qa   -> elastic/kibana/dataview_cleanup_qa
#       ccs  -> elastic/kibana/dataview_cleanup_ccs
#       prod -> elastic/kibana/dataview_cleanup_prod
#
# Alternatively set environment variables directly:
#   ES_URL       e.g. https://my-deployment.es.us-east-1.aws.found.io:9243
#   ES_API_KEY   an API key ("encoded" value)
# When --cluster/--secret-name is given, the secret's values are used.
#
# Options:
#   --cluster NAME      Load es_url/es_api_key from AWS Secrets Manager secret
#                       elastic/kibana/dataview_cleanup_<NAME> (dev|qa|ccs|prod).
#   --secret-name NAME  Override the derived AWS secret name.
#   --region NAME       AWS region for Secrets Manager (else default chain).
#   --apply             Actually delete orphans (default is dry-run).
#   --report-size       Query the _status API for the identified orphans and
#                       report how much repository storage they occupy (and how
#                       much would be reclaimed by deleting them). Read-only.
#   --repo NAME         Snapshot repository (default: found-snapshots).
#   --pattern GLOB      Only consider orphans whose snapshot name matches this
#                       shell glob (e.g. '2023.*'). Default: '*' (all).
#   --batch N           Batch size for DELETE and _status requests (default: 50).
#   -h | --help         Show this help.
#
# Examples:
#   # See every orphan in prod (safe, no changes; creds from AWS Secrets Manager):
#   ./cleanup_orphaned_searchable_snapshots.sh --cluster prod
#
#   # See the orphans AND how much storage they occupy (still read-only):
#   ./cleanup_orphaned_searchable_snapshots.sh --cluster dev --report-size
#
#   # Delete only orphaned snapshots taken in 2023:
#   ./cleanup_orphaned_searchable_snapshots.sh --cluster qa --pattern '2023.*' --apply
#
#   # Or supply credentials via environment variables instead of --cluster:
#   ES_URL=... ES_API_KEY=... ./cleanup_orphaned_searchable_snapshots.sh
# ---------------------------------------------------------------------------

set -euo pipefail

REPO="found-snapshots"
APPLY=0
REPORT_SIZE=0
PATTERN="*"
BATCH=50
CLUSTER=""
SECRET_NAME=""
REGION=""
SECRET_PREFIX="elastic/kibana/dataview_cleanup_"

usage() { sed -n '2,60p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cluster)     CLUSTER="$2"; shift 2 ;;
    --secret-name) SECRET_NAME="$2"; shift 2 ;;
    --region)      REGION="$2"; shift 2 ;;
    --apply)       APPLY=1; shift ;;
    --report-size) REPORT_SIZE=1; shift ;;
    --repo)        REPO="$2"; shift 2 ;;
    --pattern)     PATTERN="$2"; shift 2 ;;
    --batch)       BATCH="$2"; shift 2 ;;
    -h|--help)     usage 0 ;;
    *) echo "Unknown option: $1" >&2; usage 1 ;;
  esac
done

command -v jq >/dev/null   || { echo "ERROR: jq is required" >&2; exit 1; }
command -v curl >/dev/null || { echo "ERROR: curl is required" >&2; exit 1; }

# Fetch es_url + es_api_key from AWS Secrets Manager into ES_URL / ES_API_KEY.
fetch_secret() {
  local name="$1"
  command -v aws >/dev/null || {
    echo "ERROR: the aws CLI is required for --cluster/--secret-name" >&2; exit 1; }
  local region_args=()
  [[ -n "$REGION" ]] && region_args=(--region "$REGION")
  local secret_json
  secret_json="$(aws secretsmanager get-secret-value "${region_args[@]}" \
      --secret-id "$name" --query SecretString --output text)" || {
      echo "ERROR: failed to read AWS secret '$name'" >&2; exit 1; }
  ES_URL="$(printf '%s' "$secret_json" | jq -r '.es_url // empty')"
  ES_API_KEY="$(printf '%s' "$secret_json" | jq -r '.es_api_key // empty')"
  if [[ -z "$ES_URL" || -z "$ES_API_KEY" ]]; then
    echo "ERROR: AWS secret '$name' must contain 'es_url' and 'es_api_key'" >&2; exit 1
  fi
}

if [[ -n "$CLUSTER" || -n "$SECRET_NAME" ]]; then
  if [[ -n "$SECRET_NAME" ]]; then
    secret="$SECRET_NAME"
  else
    case "$CLUSTER" in
      dev|qa|ccs|prod) secret="${SECRET_PREFIX}${CLUSTER}" ;;
      *) echo "ERROR: --cluster must be one of: dev qa ccs prod" >&2; exit 1 ;;
    esac
  fi
  echo "Loading credentials from AWS secret: ${secret}" >&2
  fetch_secret "$secret"
fi

: "${ES_URL:?Set ES_URL (or use --cluster) to your Elasticsearch endpoint}"

# Build auth args for curl -- API key only.
AUTH=()
if [[ -n "${ES_API_KEY:-}" ]]; then
  AUTH=(-H "Authorization: ApiKey ${ES_API_KEY}")
else
  echo "ERROR: no API key -- set ES_API_KEY or use --cluster/--secret-name" >&2; exit 1
fi

req() {
  # req METHOD PATH  -> emits response body; fails on HTTP >= 400
  local method="$1" path="$2"; shift 2
  curl -sS --fail-with-body "${AUTH[@]}" -X "$method" "${ES_URL}${path}" "$@"
}

humanize() {
  # humanize BYTES -> human-readable (e.g. 1.50 GiB)
  jq -rn --argjson b "${1:-0}" '
    def h: if   . >= 1024*1024*1024*1024 then "\((./1024/1024/1024/1024*100|round/100)) TiB"
           elif . >= 1024*1024*1024      then "\((./1024/1024/1024*100|round/100)) GiB"
           elif . >= 1024*1024           then "\((./1024/1024*100|round/100)) MiB"
           elif . >= 1024                then "\((./1024*100|round/100)) KiB"
           else "\(.) B" end;
    $b | h'
}

echo "Cluster : ${ES_URL}"
echo "Repo    : ${REPO}"
echo "Pattern : ${PATTERN}"
echo "Mode    : $([[ $APPLY -eq 1 ]] && echo APPLY || echo DRY-RUN)$([[ $REPORT_SIZE -eq 1 ]] && echo ' +report-size')"
echo

# 1) Snapshots currently IN USE = referenced by a mounted searchable-snapshot index.
#    Mounted indices carry index.store.snapshot.snapshot_name (+ repository_name).
echo "Collecting in-use snapshots from mounted indices..."
INUSE_JSON="$(req GET "/_all/_settings/index.store.snapshot.*?flat_settings=true" || echo '{}')"
mapfile -t INUSE < <(printf '%s' "$INUSE_JSON" | jq -r --arg repo "$REPO" '
  to_entries[]
  | .value.settings as $s
  | select(($s["index.store.snapshot.repository_name"] // "") == $repo)
  | $s["index.store.snapshot.snapshot_name"] // empty
' | sort -u)
echo "  in-use snapshots: ${#INUSE[@]}"

# 2) All snapshots in the repo.
echo "Listing all snapshots in ${REPO}..."
ALL_JSON="$(req GET "/_snapshot/${REPO}/_all?ignore_unavailable=true")"
mapfile -t ALL < <(printf '%s' "$ALL_JSON" | jq -r '.snapshots[].snapshot' | sort -u)
echo "  total snapshots : ${#ALL[@]}"

# 3) Orphans = ALL - INUSE, then apply the name pattern filter.
declare -A INUSE_SET=()
for s in "${INUSE[@]:-}"; do [[ -n "$s" ]] && INUSE_SET["$s"]=1; done

ORPHANS=()
for s in "${ALL[@]:-}"; do
  [[ -z "$s" ]] && continue
  [[ -n "${INUSE_SET[$s]:-}" ]] && continue          # still referenced -> keep
  # shellcheck disable=SC2053
  [[ "$s" == $PATTERN ]] || continue                 # glob filter
  ORPHANS+=("$s")
done

echo
echo "Orphaned searchable snapshots (not referenced by any mounted index): ${#ORPHANS[@]}"
if [[ ${#ORPHANS[@]} -eq 0 ]]; then
  echo "Nothing to do."
  exit 0
fi
printf '  %s\n' "${ORPHANS[@]}"

# 3b) Optional: report the repository storage occupied by these orphans.
#     Uses the _status API (per snapshot: stats.total + stats.incremental).
#       - total       = full logical size of the snapshot.
#       - incremental = bytes this snapshot uniquely added to the repo (dedup-aware);
#                       this is the best estimate of what deletion actually reclaims.
#     NOTE: _status is a heavy, blocking call -- it is issued in batches.
if [[ $REPORT_SIZE -eq 1 ]]; then
  echo
  echo "Querying _status for ${#ORPHANS[@]} orphan(s) in batches of ${BATCH} to compute size..."
  total_bytes=0
  incr_bytes=0
  counted=0
  for ((i=0; i<${#ORPHANS[@]}; i+=BATCH)); do
    chunk=("${ORPHANS[@]:i:BATCH}")
    csv="$(IFS=,; echo "${chunk[*]}")"
    status_json="$(req GET "/_snapshot/${REPO}/${csv}/_status?ignore_unavailable=true")"
    read -r bt bi bc < <(printf '%s' "$status_json" | jq -r '
      [ .snapshots[]?.stats ] as $s
      | ( [ $s[].total.size_in_bytes ]       | add // 0 ) as $t
      | ( [ $s[].incremental.size_in_bytes ] | add // 0 ) as $inc
      | "\($t) \($inc) \(($s|length))"')
    total_bytes=$((total_bytes + bt))
    incr_bytes=$((incr_bytes + bi))
    counted=$((counted + bc))
    echo "  ...processed $((i + ${#chunk[@]}))/${#ORPHANS[@]}"
  done
  echo
  echo "==================== ORPHANED SNAPSHOT STORAGE ===================="
  echo "  snapshots measured        : ${counted}/${#ORPHANS[@]}"
  echo "  total (logical) size      : $(humanize "$total_bytes")   (${total_bytes} bytes)"
  echo "  incremental (reclaimable) : $(humanize "$incr_bytes")   (${incr_bytes} bytes)"
  echo "=================================================================="
  echo "  'incremental' is the dedup-aware estimate of space freed by deleting"
  echo "  these snapshots. For the exact repository bill, also check the backing"
  echo "  object-storage bucket metrics (S3/GCS/Azure)."
fi

if [[ $APPLY -ne 1 ]]; then
  echo
  echo "DRY-RUN: no snapshots were deleted. Re-run with --apply to delete the above."
  exit 0
fi

# 4) Delete in batches. The multi-snapshot delete API accepts a comma-separated list.
echo
echo "Deleting ${#ORPHANS[@]} snapshot(s) in batches of ${BATCH}..."
deleted=0
for ((i=0; i<${#ORPHANS[@]}; i+=BATCH)); do
  chunk=("${ORPHANS[@]:i:BATCH}")
  csv="$(IFS=,; echo "${chunk[*]}")"
  echo "  -> deleting ${#chunk[@]} snapshot(s)..."
  req DELETE "/_snapshot/${REPO}/${csv}" >/dev/null
  deleted=$((deleted + ${#chunk[@]}))
done
echo "Done. Deleted ${deleted} orphaned snapshot(s)."
