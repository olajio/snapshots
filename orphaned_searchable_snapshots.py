#!/usr/bin/env python3
"""
orphaned_searchable_snapshots.py

Find, size, and (optionally) delete ORPHANED searchable snapshots -- snapshots in
the `found-snapshots` repository that are no longer referenced by any mounted
searchable-snapshot index AND are not managed by SLM. These are the leftovers from a
frozen index being deleted (or its ILM policy changed) without ILM running the delete
phase. Snapshots created by Snapshot Lifecycle Management (SLM) -- e.g. the periodic
cloud-snapshot-* backups -- are retired by SLM's own retention, so they are never
treated as orphans.

Single tool for the whole workflow:
  * default            -> DRY-RUN: list the orphans, change nothing.
  * --report-size      -> also report how much repository storage they occupy.
  * --check-ilm        -> also flag ILM policies that will create future orphans.
  * --ilm-review-file  -> log now-compliant policies that leaked orphans in the past.
  * --apply            -> delete the orphans (dry-run unless this is given).

How it works
------------
1. Collect the set of snapshots currently IN USE, from the settings of mounted
   searchable-snapshot indices (index.store.snapshot.snapshot_name).
2. List every snapshot in the repository.
3. Orphans = all snapshots - in-use snapshots - SLM-managed snapshots (those whose
   metadata.policy names an SLM policy), optionally filtered by --pattern.
4. --report-size: sum the orphans' storage. By default this uses the Get Snapshot
   API's index_details (snapshot metadata) for the total logical size -- fast and
   safe on large repos. Add --incremental for the dedup-aware "reclaimable" size
   from the _status API (slower/heavier; --incremental implies --report-size).
5. --apply: delete the orphans.

Why not always use _status? The _status API reads every shard's file listing from
the repository (object storage) and is very slow on large repos -- on the QA repo it
timed out. index_details reads small per-index metadata blobs instead, so the fast
path avoids that. All requests also retry with backoff on read timeouts / 429 / 5xx
(see --timeout / --retries).

Snapshot names are placed in the request URL for both _status and DELETE. Because
Elasticsearch caps the HTTP request line at http.max_initial_line_length (default
4kb), requests are split into batches whose URL stays under MAX_URL_BYTES -- this
avoids the too_long_http_line_exception you hit with a naive fixed batch count.

Connection -- API key only (no basic auth)
------------------------------------------
Recommended: fetch credentials from AWS Secrets Manager with --cluster. The secret
is named `elastic/kibana/dataview_cleanup_<cluster>` and must contain the keys:
    es_url      -> the Elasticsearch endpoint
    es_api_key  -> the API key ("encoded" value)

    dev  -> elastic/kibana/dataview_cleanup_dev
    qa   -> elastic/kibana/dataview_cleanup_qa
    ccs  -> elastic/kibana/dataview_cleanup_ccs
    prod -> elastic/kibana/dataview_cleanup_prod

Resolution order for the endpoint and API key (first match wins):
    1. explicit --es-url / --api-key flags
    2. AWS Secrets Manager (when --cluster or --secret-name is given)
    3. environment variables ES_URL / ES_API_KEY

Usage
-----
  # DRY-RUN: list orphans for a cluster (credentials from AWS Secrets Manager)
  ./orphaned_searchable_snapshots.py --cluster prod

  # list + report storage occupied (read-only)
  ./orphaned_searchable_snapshots.py --cluster dev --report-size

  # actually delete only the 2023 orphans
  ./orphaned_searchable_snapshots.py --cluster qa --pattern '2023.*' --apply

  # or supply credentials directly / via environment variables
  ./orphaned_searchable_snapshots.py --es-url https://host:9243 --api-key "$KEY"
  ES_URL=... ES_API_KEY=... ./orphaned_searchable_snapshots.py

Options
-------
  --cluster {dev,qa,ccs,prod}  Load es_url/es_api_key from AWS Secrets Manager
                               secret elastic/kibana/dataview_cleanup_<cluster>.
  --secret-name NAME           Override the derived AWS secret name.
  --region NAME                AWS region for Secrets Manager (else default chain).
  --es-url URL      Elasticsearch endpoint (overrides secret and ES_URL)
  --api-key KEY     API key, "encoded" value (overrides secret and ES_API_KEY)
  --repo NAME       Snapshot repository (default: found-snapshots)
  --pattern GLOB    Only act on orphans whose name matches this glob (default: '*')
  --report-size     Report storage used by the orphans (fast; index_details metadata).
  --incremental     With --report-size, also compute the dedup-aware reclaimable size
                    via _status (slower). Implies --report-size.
  --check-ilm       Analyse ILM policies and flag culprits that create searchable
                    snapshots but won't let ILM delete them (source of future orphans).
  --apply           Delete the orphans (without this, the tool is a dry run).
  --batch N         Max snapshots per request (also bounded by URL length; default 50)
  --timeout N       Per-request read timeout in seconds (default 120)
  --retries N       Retries with backoff on read timeouts / 429 / 5xx (default 3)
  --per-snapshot    Print the largest 25 orphans with their size (implies --report-size;
                    use --json for the full per-snapshot list).
  --audit-file PATH Write the FULL orphan list plus summary/analysis to a text file (the
                    on-screen output still shows only the top 25). For audit records.
  --ilm-review-file PATH
                    Write a report of ILM policies that appear to have leaked orphans in
                    the past but are currently compliant, with each policy's last-updated
                    date. Policies with an orphan taken AFTER that date are flagged
                    NEEDS REVIEW. Implies --check-ilm.
  --frozen-usage    Estimate what % of the frozen tier's searchable-snapshot storage the
                    orphans occupy (also sizes the in-use mounted snapshots, logical).
                    Implies --report-size.
  --frozen-tier-capacity [SIZE]
                    Report reclaimable (incremental) orphan storage as a % of the TOTAL
                    frozen-tier object-store storage. Pass a size inline (e.g. 60TiB,
                    units GiB/TiB) or give the flag with no value to be prompted. Read the
                    number from the Elastic Cloud console. Implies --incremental.
  --json            Emit the report as JSON instead of text
  --insecure        Skip TLS verification (not recommended)

Requires: Python 3.7+. AWS Secrets Manager lookups use boto3 if installed, else
fall back to the `aws` CLI.
"""

import argparse
import datetime
import fnmatch
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request

SECRET_PREFIX = "elastic/kibana/dataview_cleanup_"
VALID_CLUSTERS = ("dev", "qa", "ccs", "prod")
# Snapshot names go in the request URL; keep each request line under Elasticsearch's
# http.max_initial_line_length (default 4kb / 4096 bytes). 3500 leaves safe margin.
MAX_URL_BYTES = 3500
# Sentinel meaning "prompt for the value" when --frozen-tier-capacity is given without one.
PROMPT_SENTINEL = "__PROMPT__"
# Binary (1024-based) unit factors, matching the Elastic Cloud console and human().
CAPACITY_UNITS = {"gib": 1024 ** 3, "gb": 1024 ** 3, "tib": 1024 ** 4, "tb": 1024 ** 4}


def human(n):
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if n < 1024 or unit == "PiB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def url_batches(names, path_overhead, max_count, max_url_bytes=MAX_URL_BYTES):
    """Yield lists of names so that path_overhead + len(comma-joined names) stays
    under max_url_bytes, and each batch holds at most max_count names."""
    batch = []
    length = path_overhead
    for n in names:
        add = len(n) + (1 if batch else 0)  # +1 for the comma separator
        if batch and (len(batch) >= max_count or length + add > max_url_bytes):
            yield batch
            batch = []
            length = path_overhead
            add = len(n)
        batch.append(n)
        length += add
    if batch:
        yield batch


def _get_secret_string(secret_name, region):
    """Return the raw SecretString for secret_name, via boto3 or the aws CLI."""
    try:
        import boto3  # type: ignore
        from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
    except ImportError:
        boto3 = None
    if boto3 is not None:
        try:
            client = boto3.client("secretsmanager", **({"region_name": region} if region else {}))
            return client.get_secret_value(SecretId=secret_name)["SecretString"]
        except (BotoCoreError, ClientError) as e:
            sys.exit(f"ERROR: failed to read AWS secret '{secret_name}': {e}")

    import shutil
    import subprocess
    if not shutil.which("aws"):
        sys.exit("ERROR: reading AWS Secrets Manager needs either boto3 or the aws CLI, "
                 "and neither is available.")
    cmd = ["aws", "secretsmanager", "get-secret-value",
           "--secret-id", secret_name, "--query", "SecretString", "--output", "text"]
    if region:
        cmd += ["--region", region]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except subprocess.CalledProcessError as e:
        sys.exit(f"ERROR: aws CLI failed to read secret '{secret_name}':\n{e.stderr.strip()}")


def fetch_secret_creds(secret_name, region):
    """Return (es_url, es_api_key) from an AWS Secrets Manager JSON secret."""
    raw = _get_secret_string(secret_name, region)
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        sys.exit(f"ERROR: AWS secret '{secret_name}' is not valid JSON.")
    es_url = data.get("es_url")
    es_api_key = data.get("es_api_key")
    missing = [k for k, v in (("es_url", es_url), ("es_api_key", es_api_key)) if not v]
    if missing:
        sys.exit(f"ERROR: AWS secret '{secret_name}' is missing key(s): {', '.join(missing)}. "
                 "It must contain both 'es_url' and 'es_api_key'.")
    return es_url, es_api_key


# HTTP status codes worth retrying (transient / overloaded), rather than aborting.
RETRYABLE_STATUS = {429, 502, 503, 504}


class ESClient:
    def __init__(self, url, api_key, insecure=False, timeout=120, retries=3):
        if not api_key:
            sys.exit("ERROR: no API key resolved. Use --cluster (AWS Secrets Manager), "
                     "--api-key, or the ES_API_KEY environment variable.")
        self.base = url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"ApiKey {api_key}",
        }
        self.ctx = None
        if insecure:
            self.ctx = ssl.create_default_context()
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def _request(self, method, path):
        req = urllib.request.Request(self.base + path, headers=self.headers, method=method)
        last_err = None
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(req, context=self.ctx, timeout=self.timeout) as resp:
                    body = resp.read().decode()
                    return json.loads(body) if body else {}
            except urllib.error.HTTPError as e:
                if e.code in RETRYABLE_STATUS and attempt < self.retries:
                    last_err = f"HTTP {e.code}"
                else:
                    body = e.read().decode(errors="replace")
                    sys.exit(f"ERROR: {method} {path} -> HTTP {e.code}\n{body}")
            except (socket.timeout, TimeoutError) as e:
                last_err = f"read timed out after {self.timeout}s"
                if attempt >= self.retries:
                    sys.exit(f"ERROR: {method} {path} -> {last_err} (after {self.retries + 1} attempts).\n"
                             "Try a smaller --batch, a larger --timeout, or scope with --pattern.")
            except urllib.error.URLError as e:
                # urllib wraps socket timeouts here too; retry those, fail others.
                if isinstance(e.reason, (socket.timeout, TimeoutError)) and attempt < self.retries:
                    last_err = f"read timed out after {self.timeout}s"
                else:
                    sys.exit(f"ERROR: {method} {path} -> {e.reason}")
            # Exponential backoff before the next attempt: 2s, 4s, 8s, ...
            backoff = 2 ** (attempt + 1)
            sys.stderr.write(f"  (retry {attempt + 1}/{self.retries} after {last_err}; waiting {backoff}s)\n")
            time.sleep(backoff)
        return {}

    def get(self, path):
        return self._request("GET", path)

    def delete(self, path):
        return self._request("DELETE", path)


def collect_in_use(es, repo):
    """Snapshot names referenced by mounted searchable-snapshot indices."""
    data = es.get("/_all/_settings/index.store.snapshot.*?flat_settings=true")
    in_use = set()
    for _index, body in data.items():
        s = body.get("settings", {})
        if s.get("index.store.snapshot.repository_name") == repo:
            name = s.get("index.store.snapshot.snapshot_name")
            if name:
                in_use.add(name)
    return in_use


def list_all_snapshots(es, repo):
    """Return a list of (snapshot_name, slm_policy) for every snapshot in the repo.

    slm_policy is the SLM policy managing the snapshot -- taken from the snapshot's
    metadata.policy, which Snapshot Lifecycle Management stamps onto snapshots it
    creates. It is None for snapshots not created by SLM (e.g. ILM searchable
    snapshots, manual snapshots). SLM-managed snapshots (e.g. the periodic
    cloud-snapshot-* backups) are retired by SLM's own retention, so they are NOT
    orphans even though no mounted index references them.
    """
    data = es.get(f"/_snapshot/{repo}/_all?ignore_unavailable=true")
    out = []
    for snap in data.get("snapshots", []):
        name = snap.get("snapshot")
        if not name:
            continue
        slm = (snap.get("metadata") or {}).get("policy")
        out.append((name, slm))
    return out


def size_via_index_details(es, repo, names, batch):
    """Fast sizing: total (logical) size per snapshot from the Get Snapshot API's
    index_details (snapshot metadata), NOT the heavy _status shard scan.

    Returns (total_bytes, per_snapshot dict) where per[name] = {"total": bytes}.
    index_details reads small per-index metadata blobs, so it is far cheaper than
    _status and does not time out on large repositories. It does not expose the
    dedup-aware 'incremental' figure -- use --incremental (size_via_status) for that.
    """
    overhead = len(f"/_snapshot/{repo}/") + len("?index_details=true&ignore_unavailable=true")
    total = 0
    per = {}
    done = 0
    for chunk in url_batches(names, overhead, batch):
        csv = ",".join(chunk)
        data = es.get(f"/_snapshot/{repo}/{csv}?index_details=true&ignore_unavailable=true")
        for snap in data.get("snapshots", []):
            t = sum(idx.get("size_in_bytes", 0)
                    for idx in snap.get("index_details", {}).values())
            total += t
            per[snap.get("snapshot")] = {"total": t}
        done += len(chunk)
        sys.stderr.write(f"  ...sized {done}/{len(names)}\n")
    return total, per


def size_via_status(es, repo, names, batch):
    """Precise sizing via _status: returns (total_bytes, incremental_bytes, per).
    per[name] = {"total": bytes, "incremental": bytes}. The _status API reads
    per-shard file listings from the repository and is SLOW/heavy on large repos --
    it is opt-in via --incremental and benefits from a smaller --batch."""
    overhead = len(f"/_snapshot/{repo}/") + len("/_status?ignore_unavailable=true")
    total = incr = 0
    per = {}
    done = 0
    for chunk in url_batches(names, overhead, batch):
        csv = ",".join(chunk)
        data = es.get(f"/_snapshot/{repo}/{csv}/_status?ignore_unavailable=true")
        for snap in data.get("snapshots", []):
            stats = snap.get("stats", {})
            t = stats.get("total", {}).get("size_in_bytes", 0)
            inc = stats.get("incremental", {}).get("size_in_bytes", 0)
            total += t
            incr += inc
            per[snap.get("snapshot")] = {"total": t, "incremental": inc}
        done += len(chunk)
        sys.stderr.write(f"  ...sized {done}/{len(names)}\n")
    return total, incr, per


def delete_snapshots(es, repo, names, batch):
    """Delete the given snapshots in URL-length-bounded batches. Returns count."""
    overhead = len(f"/_snapshot/{repo}/")
    deleted = 0
    for chunk in url_batches(names, overhead, batch):
        csv = ",".join(chunk)
        es.delete(f"/_snapshot/{repo}/{csv}")
        deleted += len(chunk)
        sys.stderr.write(f"  ...deleted {deleted}/{len(names)}\n")
    return deleted


def analyze_ilm_policies(es):
    """Fetch _ilm/policy and describe every policy that uses searchable_snapshot.

    Returns dict: policy_name -> {
        'modified_date': str|None,     # ILM policy's last-updated timestamp
        'ss_phases': [...],            # phases with a searchable_snapshot action
        'offending': bool,             # currently leaks (no delete phase / dss:false)
        'reason': str|None,            # why it is offending, else None
    }

    delete_searchable_snapshot defaults to True, so a policy currently leaks only when it
    takes a searchable_snapshot but (a) has no delete phase/action, or (b) sets
    delete_searchable_snapshot: false.
    """
    data = es.get("/_ilm/policy")
    out = {}
    for name, body in data.items():
        phases = body.get("policy", {}).get("phases", {})
        ss_phases = [ph for ph, cfg in phases.items()
                     if "searchable_snapshot" in cfg.get("actions", {})]
        if not ss_phases:
            continue
        delete_actions = phases.get("delete", {}).get("actions", {})
        offending, reason = False, None
        if "delete" not in delete_actions:
            offending = True
            reason = "no delete phase -> ILM never deletes the searchable snapshot"
        elif delete_actions["delete"].get("delete_searchable_snapshot", True) is False:
            offending = True
            reason = "delete phase sets delete_searchable_snapshot: false"
        out[name] = {
            "modified_date": body.get("modified_date"),
            "ss_phases": ss_phases,
            "offending": offending,
            "reason": reason,
        }
    return out


def offending_from_index(ilm_index):
    """Build the offending-policy list (shape used by print_ilm_section) from the index."""
    return [
        {"policy": n, "searchable_snapshot_phases": v["ss_phases"], "reason": v["reason"]}
        for n, v in sorted(ilm_index.items()) if v["offending"]
    ]


def _best_policy_match(name, policy_names):
    """Return the policy whose '-<policy>-' marker sits closest to the END of the snapshot
    name (rightmost wins; longer name breaks ties), or None. Searchable-snapshot names are
    {date}-{index}-{ilm-policy}-{uuid}, so the policy sits just before the uuid suffix."""
    best_p, best_pos = None, -1
    for p in policy_names:
        pos = name.rfind(f"-{p}-")
        if pos == -1:
            continue
        if pos > best_pos or (pos == best_pos and len(p) > len(best_p or "")):
            best_pos, best_p = pos, p
    return best_p


def attribute_orphans(orphans, policy_names, per_sizes=None):
    """Attribute each orphan to the policy embedded in its name.
    Returns dict: policy -> {"count": n, "bytes": b} (bytes 0 if per_sizes is None)."""
    result = {p: {"count": 0, "bytes": 0} for p in policy_names}
    for name in orphans:
        best_p = _best_policy_match(name, policy_names)
        if best_p is not None:
            result[best_p]["count"] += 1
            if per_sizes is not None:
                result[best_p]["bytes"] += per_sizes.get(name, {}).get("total", 0)
    return result


def _snapshot_date(name):
    """Leading YYYY.MM.DD of a snapshot name -> datetime.date, or None."""
    m = re.match(r"(\d{4})\.(\d{2})\.(\d{2})", name)
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _modified_date(iso):
    """Parse an ILM modified_date (e.g. 2023-06-21T13:14:53.420Z) -> datetime.date."""
    if not iso:
        return None
    try:
        return datetime.date.fromisoformat(iso[:10])
    except ValueError:
        return None


def formerly_leaking_policies(orphans, ilm_index, per_sizes=None):
    """Identify NON-offending searchable-snapshot policies that still have orphans -- i.e.
    they appear to have leaked in the past but are currently compliant.

    For each, records the policy's last-updated date, the orphan count/size, and the orphan
    date range. needs_review is True when at least one orphan was TAKEN (snapshot date)
    AFTER the policy's last update -- meaning it may still be leaking (or the index was
    deleted outside ILM), and a human should look.
    """
    policy_names = list(ilm_index.keys())
    buckets = {}
    for name in orphans:
        p = _best_policy_match(name, policy_names)
        if p is not None:
            buckets.setdefault(p, []).append(name)

    results = []
    for p, names in buckets.items():
        if ilm_index[p]["offending"]:
            continue  # current culprit -- reported in the offending section instead
        mod = _modified_date(ilm_index[p].get("modified_date"))
        dates = [d for d in (_snapshot_date(n) for n in names) if d]
        latest = max(dates) if dates else None
        earliest = min(dates) if dates else None
        total_bytes = (sum(per_sizes.get(n, {}).get("total", 0) for n in names)
                       if per_sizes is not None else None)
        results.append({
            "policy": p,
            "modified_date": ilm_index[p].get("modified_date"),
            "orphan_count": len(names),
            "orphan_bytes": total_bytes,
            "earliest_orphan": earliest.isoformat() if earliest else None,
            "latest_orphan": latest.isoformat() if latest else None,
            "needs_review": bool(mod and latest and latest > mod),
        })
    # NEEDS REVIEW first, then by orphan count desc, then name.
    results.sort(key=lambda r: (not r["needs_review"], -r["orphan_count"], r["policy"]))
    return results


def _parse_capacity(s):
    """Parse a size string like '60TiB', '60 tib', or '61440 GiB' -> (bytes, 'TiB'/'GiB').
    A bare number (no unit) returns None so the caller can ask for the unit."""
    m = re.match(r"^\s*([0-9]*\.?[0-9]+)\s*([a-zA-Z]+)\s*$", s or "")
    if not m:
        return None
    value, unit = float(m.group(1)), m.group(2).lower()
    if unit not in CAPACITY_UNITS:
        return None
    canonical = "TiB" if unit in ("tib", "tb") else "GiB"
    return int(value * CAPACITY_UNITS[unit]), canonical


def resolve_frozen_capacity(arg):
    """Resolve the frozen-tier object-store capacity from --frozen-tier-capacity.

    arg is: None (feature off), PROMPT_SENTINEL (ask interactively), or a size string.
    Returns (bytes, unit) or None. Exits with a clear message on bad/missing input.
    """
    if arg is None:
        return None
    if arg != PROMPT_SENTINEL:
        parsed = _parse_capacity(arg)
        if parsed is None:
            sys.exit(f"ERROR: could not parse --frozen-tier-capacity '{arg}'. "
                     "Use e.g. '60TiB' or '61440GiB'.")
        return parsed
    # Interactive prompt.
    if not sys.stdin.isatty():
        sys.exit("ERROR: --frozen-tier-capacity needs a value in non-interactive mode, "
                 "e.g. --frozen-tier-capacity 60TiB")
    raw = input("Enter total frozen-tier object-store storage "
                "(from the Elastic Cloud console): ").strip()
    parsed = _parse_capacity(raw)
    if parsed is None and re.match(r"^\s*[0-9]*\.?[0-9]+\s*$", raw):
        unit = input("Unit? [TiB/GiB] (default TiB): ").strip() or "TiB"
        parsed = _parse_capacity(f"{raw}{unit}")
    if parsed is None:
        sys.exit(f"ERROR: could not parse frozen-tier capacity '{raw}'. "
                 "Use e.g. '60TiB' or '61440GiB'.")
    return parsed


def resolve_credentials(args):
    """Resolve (es_url, api_key) using flags -> AWS secret -> environment."""
    url = args.es_url
    api_key = args.api_key

    if args.cluster or args.secret_name:
        secret_name = args.secret_name or (SECRET_PREFIX + args.cluster)
        sys.stderr.write(f"Loading credentials from AWS secret: {secret_name}\n")
        s_url, s_key = fetch_secret_creds(secret_name, args.region)
        url = url or s_url
        api_key = api_key or s_key

    url = url or os.environ.get("ES_URL")
    api_key = api_key or os.environ.get("ES_API_KEY")
    return url, api_key


def main():
    ap = argparse.ArgumentParser(
        description="Find, size, and optionally delete orphaned searchable snapshots.")
    ap.add_argument("--cluster", choices=VALID_CLUSTERS,
                    help="Load es_url/es_api_key from AWS Secrets Manager secret "
                         "elastic/kibana/dataview_cleanup_<cluster>.")
    ap.add_argument("--secret-name", help="Override the derived AWS secret name.")
    ap.add_argument("--region", help="AWS region for Secrets Manager (else default chain).")
    ap.add_argument("--es-url", help="Elasticsearch endpoint (overrides secret and ES_URL)")
    ap.add_argument("--api-key", help="API key, 'encoded' value (overrides secret and ES_API_KEY)")
    ap.add_argument("--repo", default="found-snapshots")
    ap.add_argument("--pattern", default="*")
    ap.add_argument("--report-size", action="store_true",
                    help="Report storage used by the orphans (read-only). Fast: uses the "
                         "Get Snapshot index_details metadata, not the heavy _status scan.")
    ap.add_argument("--incremental", action="store_true",
                    help="With --report-size, also compute the dedup-aware 'incremental' "
                         "(reclaimable) size via the _status API. Slower; can be heavy on "
                         "large repos -- pair with a smaller --batch / larger --timeout.")
    ap.add_argument("--check-ilm", action="store_true",
                    help="Also analyse ILM policies and flag 'culprit' policies that create "
                         "searchable snapshots but won't let ILM delete them (the source of "
                         "future orphans).")
    ap.add_argument("--apply", action="store_true",
                    help="Delete the orphans (without this, the tool is a dry run).")
    ap.add_argument("--batch", type=int, default=50,
                    help="Max snapshots per request (also bounded by URL length).")
    ap.add_argument("--timeout", type=int, default=120,
                    help="Per-request read timeout in seconds (default 120).")
    ap.add_argument("--retries", type=int, default=3,
                    help="Retries with exponential backoff on read timeouts / 429 / 5xx (default 3).")
    ap.add_argument("--per-snapshot", action="store_true",
                    help="Print the largest 25 orphans with their size (implies "
                         "--report-size; use --json for the full list).")
    ap.add_argument("--audit-file", metavar="PATH",
                    help="Write the FULL orphan list plus summary and analysis to this text "
                         "file (the on-screen output still shows only the top 25).")
    ap.add_argument("--ilm-review-file", metavar="PATH",
                    help="Write a report of ILM policies that appear to have leaked orphans "
                         "in the past but are currently compliant, with each policy's last "
                         "update date. Policies with an orphan taken AFTER that date are "
                         "flagged NEEDS REVIEW. Implies --check-ilm.")
    ap.add_argument("--frozen-usage", action="store_true",
                    help="Estimate what percentage of the frozen tier's searchable-snapshot "
                         "storage the orphans occupy (also sizes the in-use mounted snapshots). "
                         "Uses logical sizes; heavier on clusters with many mounted snapshots. "
                         "Implies --report-size.")
    ap.add_argument("--frozen-tier-capacity", metavar="SIZE", nargs="?", const=PROMPT_SENTINEL,
                    help="Report reclaimable (incremental) orphan storage as a percentage of "
                         "the TOTAL frozen-tier object-store storage. Give the size inline "
                         "(e.g. --frozen-tier-capacity 60TiB, units GiB/TiB) or pass the flag "
                         "with no value to be prompted at runtime. Read this number from the "
                         "Elastic Cloud console (the ES API cannot supply it). Implies "
                         "--incremental.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--insecure", action="store_true")
    args = ap.parse_args()

    if args.incremental or args.per_snapshot or args.frozen_usage:
        args.report_size = True  # these only make sense while reporting size
    if args.frozen_tier_capacity is not None:
        args.incremental = True   # the numerator is the dedup-aware reclaimable size
        args.report_size = True
    if args.ilm_review_file:
        args.check_ilm = True  # the review report needs the ILM policy data

    # Resolve (and, if needed, prompt for) the frozen-tier capacity up front, so the
    # interactive prompt is answered before the potentially long scan begins.
    frozen_capacity = resolve_frozen_capacity(args.frozen_tier_capacity)

    url, api_key = resolve_credentials(args)
    if not url:
        sys.exit("ERROR: no Elasticsearch endpoint resolved. Use --cluster "
                 "(AWS Secrets Manager), --es-url, or the ES_URL environment variable.")
    es = ESClient(url, api_key=api_key, insecure=args.insecure,
                  timeout=args.timeout, retries=args.retries)

    mode = "APPLY (delete)" if args.apply else "DRY-RUN"
    sys.stderr.write(f"Repo    : {args.repo}\nPattern : {args.pattern}\nMode    : {mode}\n")
    sys.stderr.write("Collecting in-use snapshots from mounted indices...\n")
    in_use = collect_in_use(es, args.repo)
    sys.stderr.write(f"  in-use snapshots: {len(in_use)}\n")

    sys.stderr.write(f"Listing all snapshots in {args.repo}...\n")
    all_snaps = list_all_snapshots(es, args.repo)
    sys.stderr.write(f"  total snapshots : {len(all_snaps)}\n")

    # SLM-managed snapshots (metadata.policy set) are retired by SLM's own retention,
    # so they are never orphans -- exclude them from the candidate set.
    slm_managed = {name for name, slm in all_snaps if slm}
    if slm_managed:
        sys.stderr.write(f"  SLM-managed (excluded): {len(slm_managed)}\n")

    all_names = sorted({name for name, _ in all_snaps})
    orphans = [s for s in all_names
               if s not in in_use
               and s not in slm_managed
               and fnmatch.fnmatch(s, args.pattern)]
    sys.stderr.write(f"  orphaned (match): {len(orphans)}\n")

    report = {
        "repo": args.repo,
        "pattern": args.pattern,
        "total_snapshots": len(all_snaps),
        "in_use": len(in_use),
        "slm_managed_excluded": len(slm_managed),
        "orphan_count": len(orphans),
        "applied": bool(args.apply),
    }

    # Optional: analyse ILM policies for culprits that will create future orphans.
    ilm_index = None
    if args.check_ilm:
        sys.stderr.write("Analysing ILM policies for searchable-snapshot culprits...\n")
        ilm_index = analyze_ilm_policies(es)
        report["offending_ilm_policies"] = offending_from_index(ilm_index)
        sys.stderr.write(f"  offending ILM policies: {len(report['offending_ilm_policies'])}\n")

    if not orphans:
        if args.ilm_review_file and ilm_index is not None:
            write_ilm_review_file(args.ilm_review_file, args,
                                  formerly_leaking_policies([], ilm_index, None))
        if args.audit_file:
            write_audit_file(args.audit_file, args, report, [], None)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print("No orphaned snapshots found.")
            print_ilm_section(args, report)
        return

    # List the orphans (to stderr so stdout stays clean for the report / JSON).
    for name in orphans:
        sys.stderr.write(f"  orphan: {name}\n")

    # Optional: size the orphans.
    per = None
    if args.report_size:
        if args.incremental:
            sys.stderr.write(f"Sizing {len(orphans)} orphan(s) via _status "
                             f"(dedup-aware, batch<= {args.batch})...\n")
            total, incr, per = size_via_status(es, args.repo, orphans, args.batch)
            report["incremental_bytes"] = incr
            report["incremental_human"] = human(incr)
        else:
            sys.stderr.write(f"Sizing {len(orphans)} orphan(s) via index_details "
                             f"(fast, batch<= {args.batch})...\n")
            total, per = size_via_index_details(es, args.repo, orphans, args.batch)
        report.update({
            "measured_count": len(per),
            "total_bytes": total,
            "total_human": human(total),
            "size_method": "status" if args.incremental else "index_details",
        })
        if args.per_snapshot:
            report["per_snapshot"] = [
                dict({"snapshot": n, "total_bytes": v["total"]},
                     **({"incremental_bytes": v["incremental"]} if "incremental" in v else {}))
                for n, v in sorted(per.items(), key=lambda kv: kv[1]["total"], reverse=True)
            ]

    # Optional: what share of the frozen tier's searchable-snapshot storage is orphaned?
    # Frozen-tier snapshot storage = the snapshots backing mounted (in-use) searchable
    # indices + the orphans. Sizes are LOGICAL (index_details) so the ratio is a consistent
    # share of frozen-tier data; the exact freed-on-delete space is the dedup-aware
    # 'incremental' figure, which can be much smaller.
    if args.frozen_usage:
        in_use_names = sorted(in_use)
        sys.stderr.write(f"Sizing {len(in_use_names)} in-use searchable snapshot(s) for "
                         f"frozen-tier % (index_details)...\n")
        in_use_total, _ = size_via_index_details(es, args.repo, in_use_names, args.batch)
        orphan_total = report.get("total_bytes", 0)
        frozen_total = orphan_total + in_use_total
        pct = (100.0 * orphan_total / frozen_total) if frozen_total else 0.0
        report["frozen_tier"] = {
            "basis": "logical (index_details)",
            "in_use_count": len(in_use_names),
            "in_use_bytes": in_use_total, "in_use_human": human(in_use_total),
            "orphan_bytes": orphan_total, "orphan_human": human(orphan_total),
            "total_bytes": frozen_total, "total_human": human(frozen_total),
            "orphan_pct_of_frozen": round(pct, 2),
        }
        sys.stderr.write(f"  frozen-tier orphan share: {pct:.2f}%  "
                         f"({human(orphan_total)} of {human(frozen_total)})\n")

    # Optional: reclaimable orphan bytes as a % of the TOTAL frozen-tier object-store
    # storage (a number supplied by the user -- the ES API cannot provide it). The
    # numerator is the dedup-aware 'incremental' size (what actually frees on deletion).
    if frozen_capacity is not None:
        cap_bytes, cap_unit = frozen_capacity
        reclaimable = report.get("incremental_bytes", 0)
        pct = (100.0 * reclaimable / cap_bytes) if cap_bytes else 0.0
        report["frozen_reclaim"] = {
            "capacity_bytes": cap_bytes,
            "capacity_unit": cap_unit,
            "capacity_human": human(cap_bytes),
            "reclaimable_bytes": reclaimable,
            "reclaimable_human": human(reclaimable),
            "reclaimable_pct_of_capacity": round(pct, 2),
        }
        sys.stderr.write(f"  reclaimable is {pct:.2f}% of frozen-tier object-store storage "
                         f"({human(reclaimable)} of {human(cap_bytes)})\n")

    # Optional: attribute orphans to the offending ILM policies that produced them.
    if args.check_ilm and report.get("offending_ilm_policies"):
        attrib = attribute_orphans(
            orphans, [p["policy"] for p in report["offending_ilm_policies"]], per)
        for p in report["offending_ilm_policies"]:
            a = attrib[p["policy"]]
            p["orphan_count"] = a["count"]
            if args.report_size:
                p["orphan_bytes"] = a["bytes"]
                p["orphan_human"] = human(a["bytes"])

    # Optional: log formerly-leaking-but-now-compliant ILM policies to a review file.
    if args.ilm_review_file and ilm_index is not None:
        review = formerly_leaking_policies(orphans, ilm_index, per)
        report["formerly_leaking_ilm_policies"] = review
        write_ilm_review_file(args.ilm_review_file, args, review)

    # Optional: delete the orphans.
    if args.apply:
        sys.stderr.write(f"Deleting {len(orphans)} orphan(s) (batch<= {args.batch})...\n")
        report["deleted"] = delete_snapshots(es, args.repo, orphans, args.batch)

    # Optional: write the FULL orphan list + analysis to an audit file.
    if args.audit_file:
        write_audit_file(args.audit_file, args, report, orphans, per)

    if args.json:
        print(json.dumps(report, indent=2))
        return

    # Human-readable summary.
    print("\n==================== ORPHANED SEARCHABLE SNAPSHOTS ====================")
    print(f"  repository         : {args.repo}")
    print(f"  name pattern       : {args.pattern}")
    print(f"  orphaned snapshots : {len(orphans)}")
    if args.report_size:
        print(f"  total (logical)    : {report['total_human']}   ({report['total_bytes']} bytes)")
        if args.incremental:
            print(f"  incremental (free) : {report['incremental_human']}   ({report['incremental_bytes']} bytes)")
    print("======================================================================")
    if args.report_size:
        print(f"  size method        : {report['size_method']}")
        if args.incremental:
            print("  'incremental (free)' is the dedup-aware estimate of space freed by")
            print("  deleting these snapshots.")
        else:
            print("  'total (logical)' sums each snapshot's index sizes (upper bound; shared")
            print("  blobs counted once per snapshot). Add --incremental for the dedup-aware")
            print("  reclaimable figure.")
        print("  For the exact repository bill, also check the backing object-storage")
        print("  bucket metrics (S3/GCS/Azure).")
    if "frozen_tier" in report:
        ft = report["frozen_tier"]
        print("\n  -- FROZEN-TIER STORAGE (logical) --")
        print(f"  in-use searchable snapshots : {ft['in_use_human']}  ({ft['in_use_count']} snapshots)")
        print(f"  orphaned searchable snapshots: {ft['orphan_human']}")
        print(f"  total frozen-tier snapshots  : {ft['total_human']}")
        print(f"  >> orphans are {ft['orphan_pct_of_frozen']:.2f}% of frozen-tier "
              "searchable-snapshot storage (logical)")
        print("  Note: logical share; space actually freed on deletion is the dedup-aware")
        print("  'incremental' figure (run --incremental) and can be much smaller when orphans")
        print("  share blobs with live snapshots.")
    if "frozen_reclaim" in report:
        fr = report["frozen_reclaim"]
        print("\n  -- RECLAIMABLE vs FROZEN-TIER OBJECT-STORE STORAGE --")
        print(f"  frozen-tier object-store storage : {fr['capacity_human']}  (provided)")
        print(f"  reclaimable (incremental) orphans: {fr['reclaimable_human']}")
        print(f"  >> deleting orphans reclaims {fr['reclaimable_pct_of_capacity']:.2f}% of "
              "total frozen-tier object-store storage")
    if args.apply:
        print(f"  DELETED {report['deleted']} orphaned snapshot(s).")
    else:
        print("  DRY-RUN: nothing deleted. Re-run with --apply to delete the above.")
    if args.report_size and args.per_snapshot:
        rows = report["per_snapshot"]
        has_incr = any("incremental_bytes" in r for r in rows)
        cols = "total, incremental" if has_incr else "total"
        shown = min(25, len(rows))
        print(f"\n  Largest {shown} orphan(s) by size ({cols}):")
        for row in rows[:25]:
            size = f"{human(row['total_bytes']):>12}"
            if has_incr:
                size += f"  {human(row.get('incremental_bytes', 0)):>12}"
            print(f"    {size}  {row['snapshot']}")
        if len(rows) > 25:
            print(f"    ... and {len(rows) - 25} more (use --json for the full list)")

    print_ilm_section(args, report)


def print_ilm_section(args, report):
    """Print the offending-ILM-policies section (text mode) if --check-ilm ran."""
    if not args.check_ilm:
        return
    offending = report.get("offending_ilm_policies", [])
    print("\n==================== OFFENDING ILM POLICIES ==========================")
    if not offending:
        print("  None. Every policy that creates searchable snapshots also lets ILM")
        print("  delete them (delete phase present, delete_searchable_snapshot not false).")
        print("======================================================================")
        return
    print(f"  {len(offending)} policy(ies) create searchable snapshots that ILM will NOT")
    print("  clean up -- these are the source of future orphans:")
    attributed_bytes = 0
    for p in offending:
        phases = ",".join(p["searchable_snapshot_phases"])
        print(f"    - {p['policy']}  (searchable_snapshot in: {phases})")
        print(f"        {p['reason']}")
        if "orphan_count" in p:
            if "orphan_human" in p:
                attributed_bytes += p.get("orphan_bytes", 0)
                print(f"        orphaned snapshots from this policy: {p['orphan_count']}"
                      f"  ({p['orphan_human']})")
            else:
                print(f"        orphaned snapshots from this policy: {p['orphan_count']}"
                      "  (add --report-size for their size)")
    print("======================================================================")
    if any("orphan_human" in p for p in offending):
        print(f"  total orphaned storage attributable to these policies: {human(attributed_bytes)}")
    print("  Fix: add a delete phase with delete_searchable_snapshot: true (its default),")
    print("  or set it to true where it is currently false.")


def write_audit_file(path, args, report, orphans, per):
    """Write the FULL orphan list plus a summary/analysis to a text file for auditing.
    Unlike the on-screen output (top 25), this lists EVERY orphan."""
    L = []
    def w(s=""):
        L.append(s)

    w("=" * 70)
    w("ORPHANED SEARCHABLE SNAPSHOT AUDIT")
    w("=" * 70)
    w(f"Generated (UTC) : {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
    w(f"Cluster         : {args.cluster or '(from --es-url / ES_URL)'}")
    w(f"Repository      : {report['repo']}")
    w(f"Name pattern    : {report['pattern']}")
    w(f"Mode            : {'APPLY (snapshots deleted)' if args.apply else 'DRY-RUN (nothing deleted)'}")
    w("")
    w("SUMMARY")
    w(f"  total snapshots in repo : {report.get('total_snapshots', '?')}")
    w(f"  in-use (mounted)        : {report.get('in_use', '?')}")
    w(f"  SLM-managed (excluded)  : {report.get('slm_managed_excluded', 0)}")
    w(f"  orphaned snapshots      : {report['orphan_count']}")
    if "total_bytes" in report:
        w(f"  total (logical)         : {report['total_human']}  ({report['total_bytes']} bytes)")
        if "incremental_bytes" in report:
            w(f"  incremental (reclaim)   : {report['incremental_human']}  ({report['incremental_bytes']} bytes)")
        w(f"  size method             : {report.get('size_method')}")
    if "deleted" in report:
        w(f"  deleted                 : {report['deleted']}")
    if "frozen_tier" in report:
        ft = report["frozen_tier"]
        w("")
        w("FROZEN-TIER STORAGE (logical, from index_details)")
        w(f"  in-use searchable snapshots  : {ft['in_use_human']}  ({ft['in_use_count']} snapshots)")
        w(f"  orphaned searchable snapshots: {ft['orphan_human']}")
        w(f"  total frozen-tier snapshots  : {ft['total_human']}")
        w(f"  orphans are {ft['orphan_pct_of_frozen']:.2f}% of frozen-tier searchable-snapshot storage")
        w("  (logical share; actual freed space on deletion is the dedup-aware 'incremental'")
        w("   figure and can be much smaller when orphans share blobs with live snapshots.)")
    if "frozen_reclaim" in report:
        fr = report["frozen_reclaim"]
        w("")
        w("RECLAIMABLE vs FROZEN-TIER OBJECT-STORE STORAGE")
        w(f"  frozen-tier object-store storage : {fr['capacity_human']}  (provided)")
        w(f"  reclaimable (incremental) orphans: {fr['reclaimable_human']}")
        w(f"  reclaimable is {fr['reclaimable_pct_of_capacity']:.2f}% of total frozen-tier "
          "object-store storage")
    w("")

    if args.check_ilm:
        off = report.get("offending_ilm_policies", [])
        w("OFFENDING ILM POLICIES (create searchable snapshots ILM will not delete)")
        if not off:
            w("  None.")
        for p in off:
            phases = ",".join(p["searchable_snapshot_phases"])
            w(f"  - {p['policy']}  (searchable_snapshot in: {phases}); {p['reason']}")
            if "orphan_count" in p:
                extra = f"  ({p['orphan_human']})" if "orphan_human" in p else ""
                w(f"      orphans from this policy: {p['orphan_count']}{extra}")
        w("")

    w("NOTES")
    w("  - Orphan = in repo, not referenced by any mounted index, and NOT SLM-managed")
    w("    (SLM-managed snapshots are retired by SLM's own retention).")
    w("  - 'total (logical)' sums each snapshot's index sizes and OVERCOUNTS blobs shared")
    w("    between snapshots -- treat it as an upper bound.")
    w("  - 'incremental (reclaim)' (only with --incremental) is the dedup-aware estimate of")
    w("    space freed on deletion; actual freed can be lower when blobs are shared with")
    w("    retained (in-use or SLM) snapshots. The exact figure is the object-store bucket")
    w("    size before vs. after.")
    w("")

    if per:
        rows = sorted(orphans, key=lambda n: per.get(n, {}).get("total", 0), reverse=True)
        has_incr = any("incremental" in per.get(n, {}) for n in orphans)
        w(f"FULL ORPHAN LIST ({len(orphans)}) -- largest first:")
        head = f"  {'total':>12}"
        if has_incr:
            head += f"  {'incremental':>12}"
        head += "  snapshot"
        w(head)
        for n in rows:
            v = per.get(n, {})
            line = f"  {human(v.get('total', 0)):>12}"
            if has_incr:
                line += f"  {human(v.get('incremental', 0)):>12}"
            line += f"  {n}"
            w(line)
    else:
        w(f"FULL ORPHAN LIST ({len(orphans)}):")
        for n in sorted(orphans):
            w(f"  {n}")
    w("")

    try:
        with open(path, "w") as fh:
            fh.write("\n".join(L) + "\n")
        sys.stderr.write(f"Audit file written: {path} ({len(orphans)} orphan(s))\n")
    except OSError as e:
        sys.stderr.write(f"WARNING: could not write audit file '{path}': {e}\n")


def write_ilm_review_file(path, args, review):
    """Write the formerly-leaking-but-now-compliant ILM policy report to a text file."""
    L = []
    def w(s=""):
        L.append(s)

    needs = [r for r in review if r["needs_review"]]
    ok = [r for r in review if not r["needs_review"]]

    def fmt(r):
        sz = human(r["orphan_bytes"]) if r.get("orphan_bytes") is not None else "n/a"
        dates = f"{r['earliest_orphan']} .. {r['latest_orphan']}"
        return sz, dates

    w("=" * 70)
    w("ILM POLICIES THAT APPEAR TO HAVE LEAKED SEARCHABLE SNAPSHOTS")
    w("(currently compliant -- delete_searchable_snapshot is NOT disabled)")
    w("=" * 70)
    w(f"Generated (UTC) : {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
    w(f"Cluster         : {args.cluster or '(from --es-url / ES_URL)'}")
    w("")
    w("A policy is listed here if it currently HAS a delete phase with")
    w("delete_searchable_snapshot enabled, yet orphaned searchable snapshots exist whose")
    w("name embeds this policy -- i.e. it leaked in the past but looks compliant now")
    w("(likely updated after the leak).")
    w("")
    w("'NEEDS REVIEW' = at least one leaked snapshot was TAKEN (snapshot date) AFTER the")
    w("policy's last update, so it may still be leaking, or the index was deleted outside")
    w("ILM. Those should be reviewed by a human.")
    w("")

    w(f"NEEDS REVIEW -- leaked AFTER last update ({len(needs)}):")
    if not needs:
        w("  (none)")
    for r in needs:
        sz, dates = fmt(r)
        w(f"  - {r['policy']}")
        w(f"      last updated : {r['modified_date']}")
        w(f"      orphans      : {r['orphan_count']}  ({sz})")
        w(f"      orphan dates : {dates}")
        w(f"      !! latest orphan {r['latest_orphan']} is AFTER last update "
          f"{(r['modified_date'] or '')[:10]} -> REVIEW")
    w("")

    w(f"LIKELY ALREADY FIXED -- all leaked snapshots predate last update ({len(ok)}):")
    if not ok:
        w("  (none)")
    for r in ok:
        sz, dates = fmt(r)
        w(f"  - {r['policy']}  (last updated {(r['modified_date'] or 'unknown')[:10]}; "
          f"orphans {r['orphan_count']}, {sz}; dates {dates})")
    w("")

    try:
        with open(path, "w") as fh:
            fh.write("\n".join(L) + "\n")
        sys.stderr.write(f"ILM review file written: {path} "
                         f"({len(needs)} need review, {len(ok)} likely fixed)\n")
    except OSError as e:
        sys.stderr.write(f"WARNING: could not write ILM review file '{path}': {e}\n")


if __name__ == "__main__":
    main()
