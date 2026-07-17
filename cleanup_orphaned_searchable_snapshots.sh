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
# Requirements: bash, curl, jq
#
# Configure the target cluster via environment variables:
#   ES_URL       e.g. https://my-deployment.es.us-east-1.aws.found.io:9243
#   ES_API_KEY   an API key (id:key base64, i.e. the "encoded" value)   -- OR --
#   ES_USER / ES_PASS   basic-auth credentials
#
# Options:
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
#   # See every orphan in the repo (safe, no changes):
#   ES_URL=... ES_API_KEY=... ./cleanup_orphaned_searchable_snapshots.sh
#
#   # See the orphans AND how much storage they occupy (still read-only):
#   ES_URL=... ES_API_KEY=... ./cleanup_orphaned_searchable_snapshots.sh --report-size
#
#   # Delete only orphaned snapshots taken in 2023:
#   ES_URL=... ES_API_KEY=... ./cleanup_orphaned_searchable_snapshots.sh --pattern '2023.*' --apply
# ---------------------------------------------------------------------------

set -euo pipefail

REPO="found-snapshots"
APPLY=0
REPORT_SIZE=0
PATTERN="*"
BATCH=50

usage() { sed -n '2,50p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
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
: "${ES_URL:?Set ES_URL to your Elasticsearch endpoint}"

# Build auth args for curl.
AUTH=()
if [[ -n "${ES_API_KEY:-}" ]]; then
  AUTH=(-H "Authorization: ApiKey ${ES_API_KEY}")
elif [[ -n "${ES_USER:-}" && -n "${ES_PASS:-}" ]]; then
  AUTH=(-u "${ES_USER}:${ES_PASS}")
else
  echo "ERROR: set ES_API_KEY, or ES_USER and ES_PASS" >&2; exit 1
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
