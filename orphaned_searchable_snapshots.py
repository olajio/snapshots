#!/usr/bin/env python3
"""
orphaned_searchable_snapshots.py

Find, size, and (optionally) delete ORPHANED searchable snapshots -- snapshots in
the `found-snapshots` repository that are no longer referenced by any mounted
searchable-snapshot index. These are the leftovers from a frozen index being
deleted (or its ILM policy changed) without ILM running the delete phase.

Single tool for the whole workflow:
  * default            -> DRY-RUN: list the orphans, change nothing.
  * --report-size      -> also report how much repository storage they occupy.
  * --check-ilm        -> also flag ILM policies that will create future orphans.
  * --apply            -> delete the orphans (dry-run unless this is given).

How it works
------------
1. Collect the set of snapshots currently IN USE, from the settings of mounted
   searchable-snapshot indices (index.store.snapshot.snapshot_name).
2. List every snapshot in the repository.
3. Orphans = all snapshots - in-use snapshots (optionally filtered by --pattern).
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
  --per-snapshot    List every orphan with its individual size (largest first).
                    Implies --report-size.
  --json            Emit the report as JSON instead of text
  --insecure        Skip TLS verification (not recommended)

Requires: Python 3.7+. AWS Secrets Manager lookups use boto3 if installed, else
fall back to the `aws` CLI.
"""

import argparse
import fnmatch
import json
import os
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
    data = es.get(f"/_snapshot/{repo}/_all?ignore_unavailable=true")
    return sorted({snap["snapshot"] for snap in data.get("snapshots", [])})


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


def find_offending_ilm_policies(es):
    """Return the ILM policies that CREATE searchable snapshots but will not let ILM
    delete them -- the sources of future orphans.

    delete_searchable_snapshot defaults to True, so a policy leaks only when it takes
    a searchable_snapshot but (a) has no delete phase/action, or (b) sets
    delete_searchable_snapshot: false. Returns a list of dicts sorted by name.
    """
    data = es.get("/_ilm/policy")
    offending = []
    for name, body in sorted(data.items()):
        phases = body.get("policy", {}).get("phases", {})
        ss_phases = [ph for ph, cfg in phases.items()
                     if "searchable_snapshot" in cfg.get("actions", {})]
        if not ss_phases:
            continue
        delete_actions = phases.get("delete", {}).get("actions", {})
        if "delete" not in delete_actions:
            offending.append({
                "policy": name, "searchable_snapshot_phases": ss_phases,
                "reason": "no delete phase -> ILM never deletes the searchable snapshot",
                "delete_searchable_snapshot": None,
            })
        else:
            dss = delete_actions["delete"].get("delete_searchable_snapshot", True)
            if dss is False:
                offending.append({
                    "policy": name, "searchable_snapshot_phases": ss_phases,
                    "reason": "delete phase sets delete_searchable_snapshot: false",
                    "delete_searchable_snapshot": False,
                })
    return offending


def attribute_orphans(orphans, policy_names, per_sizes=None):
    """Attribute each orphan to the offending ILM policy embedded in its name.

    Searchable-snapshot names follow {date}-{index}-{ilm-policy}-{uuid}, so the policy
    sits just before the uuid suffix. For each orphan we pick the offending policy whose
    '-<policy>-' marker occurs closest to the END of the name (rightmost wins; longer name
    breaks ties) to avoid matching the same token inside the index portion.

    Returns dict: policy -> {"count": n, "bytes": b} (bytes 0 if per_sizes is None).
    """
    result = {p: {"count": 0, "bytes": 0} for p in policy_names}
    for name in orphans:
        best_p, best_pos = None, -1
        for p in policy_names:
            pos = name.rfind(f"-{p}-")
            if pos == -1:
                continue
            if pos > best_pos or (pos == best_pos and len(p) > len(best_p or "")):
                best_pos, best_p = pos, p
        if best_p is not None:
            result[best_p]["count"] += 1
            if per_sizes is not None:
                result[best_p]["bytes"] += per_sizes.get(name, {}).get("total", 0)
    return result


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
                    help="List every orphan with its individual size (largest first). "
                         "Implies --report-size.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--insecure", action="store_true")
    args = ap.parse_args()

    if args.incremental or args.per_snapshot:
        args.report_size = True  # both only make sense while reporting size

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

    orphans = [s for s in all_snaps
               if s not in in_use and fnmatch.fnmatch(s, args.pattern)]
    sys.stderr.write(f"  orphaned (match): {len(orphans)}\n")

    report = {
        "repo": args.repo,
        "pattern": args.pattern,
        "orphan_count": len(orphans),
        "applied": bool(args.apply),
    }

    # Optional: analyse ILM policies for culprits that will create future orphans.
    if args.check_ilm:
        sys.stderr.write("Analysing ILM policies for searchable-snapshot culprits...\n")
        offending = find_offending_ilm_policies(es)
        report["offending_ilm_policies"] = offending
        sys.stderr.write(f"  offending ILM policies: {len(offending)}\n")

    if not orphans:
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

    # Optional: delete the orphans.
    if args.apply:
        sys.stderr.write(f"Deleting {len(orphans)} orphan(s) (batch<= {args.batch})...\n")
        report["deleted"] = delete_snapshots(es, args.repo, orphans, args.batch)

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
    if args.apply:
        print(f"  DELETED {report['deleted']} orphaned snapshot(s).")
    else:
        print("  DRY-RUN: nothing deleted. Re-run with --apply to delete the above.")
    if args.report_size and args.per_snapshot:
        rows = report["per_snapshot"]
        has_incr = any("incremental_bytes" in r for r in rows)
        header = "size (total"
        header += ", incremental)" if has_incr else ")"
        print(f"\n  Per-snapshot {header}, largest first -- {len(rows)} orphan(s):")
        for row in rows:
            size = f"{human(row['total_bytes']):>12}"
            if has_incr:
                size += f"  {human(row.get('incremental_bytes', 0)):>12}"
            print(f"    {size}  {row['snapshot']}")

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


if __name__ == "__main__":
    main()
