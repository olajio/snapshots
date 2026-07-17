#!/usr/bin/env python3
"""
orphaned_snapshot_size_report.py

Report the total repository storage occupied by ORPHANED searchable snapshots --
snapshots in the `found-snapshots` repository that are no longer referenced by any
mounted searchable-snapshot index.

This is a READ-ONLY reporting tool. It never deletes anything. It complements
cleanup_orphaned_searchable_snapshots.sh by focusing purely on sizing.

How it works
------------
1. Collect the set of snapshots currently IN USE, from the settings of mounted
   searchable-snapshot indices (index.store.snapshot.snapshot_name).
2. List every snapshot in the repository.
3. Orphans = all snapshots - in-use snapshots (optionally filtered by --pattern).
4. Query the _status API (in batches) for those orphans and sum:
     - total.size_in_bytes        (full logical size)
     - incremental.size_in_bytes  (dedup-aware -> best estimate of space reclaimed)

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
  # load endpoint + API key from AWS Secrets Manager for a cluster (recommended)
  ./orphaned_snapshot_size_report.py --cluster prod
  ./orphaned_snapshot_size_report.py --cluster dev --pattern '2023.*'
  ./orphaned_snapshot_size_report.py --cluster qa --json > report.json

  # or supply them directly / via environment variables
  ./orphaned_snapshot_size_report.py --es-url https://host:9243 --api-key "$KEY"
  ES_URL=... ES_API_KEY=... ./orphaned_snapshot_size_report.py

Options
-------
  --cluster {dev,qa,ccs,prod}  Load es_url/es_api_key from AWS Secrets Manager
                               secret elastic/kibana/dataview_cleanup_<cluster>.
  --secret-name NAME           Override the derived AWS secret name.
  --region NAME                AWS region for Secrets Manager (else default chain).
  --es-url URL      Elasticsearch endpoint (overrides secret and ES_URL)
  --api-key KEY     API key, "encoded" value (overrides secret and ES_API_KEY)
  --repo NAME       Snapshot repository (default: found-snapshots)
  --pattern GLOB    Only size orphans whose name matches this glob (default: '*')
  --batch N         Snapshots per _status request (default: 50)
  --per-snapshot    Also print a per-snapshot size breakdown (largest first)
  --json            Emit the report as JSON instead of text
  --insecure        Skip TLS verification (not recommended)

Requires: Python 3.7+. AWS Secrets Manager lookups use boto3 if installed, else
fall back to the `aws` CLI. No third-party packages are needed unless you use
--cluster/--secret-name without the aws CLI available.
"""

import argparse
import fnmatch
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

SECRET_PREFIX = "elastic/kibana/dataview_cleanup_"
VALID_CLUSTERS = ("dev", "qa", "ccs", "prod")


def human(n):
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if n < 1024 or unit == "PiB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def _get_secret_string(secret_name, region):
    """Return the raw SecretString for secret_name, via boto3 or the aws CLI."""
    # Preferred path: boto3.
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

    # Fallback: the aws CLI.
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


class ESClient:
    def __init__(self, url, api_key, insecure=False):
        if not api_key:
            sys.exit("ERROR: no API key resolved. Use --cluster (AWS Secrets Manager), "
                     "--api-key, or the ES_API_KEY environment variable.")
        self.base = url.rstrip("/")
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"ApiKey {api_key}",
        }
        self.ctx = None
        if insecure:
            self.ctx = ssl.create_default_context()
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def get(self, path):
        req = urllib.request.Request(self.base + path, headers=self.headers, method="GET")
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=300) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            sys.exit(f"ERROR: GET {path} -> HTTP {e.code}\n{body}")
        except urllib.error.URLError as e:
            sys.exit(f"ERROR: GET {path} -> {e.reason}")


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


def size_snapshots(es, repo, names, batch):
    """Return (total_bytes, incremental_bytes, per_snapshot dict) via _status."""
    total = incr = 0
    per = {}
    for i in range(0, len(names), batch):
        chunk = names[i:i + batch]
        csv = ",".join(chunk)
        data = es.get(f"/_snapshot/{repo}/{csv}/_status?ignore_unavailable=true")
        for snap in data.get("snapshots", []):
            stats = snap.get("stats", {})
            t = stats.get("total", {}).get("size_in_bytes", 0)
            inc = stats.get("incremental", {}).get("size_in_bytes", 0)
            total += t
            incr += inc
            per[snap.get("snapshot")] = {"total": t, "incremental": inc}
        sys.stderr.write(f"  ...sized {min(i + batch, len(names))}/{len(names)}\n")
    return total, incr, per


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
    ap = argparse.ArgumentParser(description="Report storage used by orphaned searchable snapshots.")
    ap.add_argument("--cluster", choices=VALID_CLUSTERS,
                    help="Load es_url/es_api_key from AWS Secrets Manager secret "
                         "elastic/kibana/dataview_cleanup_<cluster>.")
    ap.add_argument("--secret-name", help="Override the derived AWS secret name.")
    ap.add_argument("--region", help="AWS region for Secrets Manager (else default chain).")
    ap.add_argument("--es-url", help="Elasticsearch endpoint (overrides secret and ES_URL)")
    ap.add_argument("--api-key", help="API key, 'encoded' value (overrides secret and ES_API_KEY)")
    ap.add_argument("--repo", default="found-snapshots")
    ap.add_argument("--pattern", default="*")
    ap.add_argument("--batch", type=int, default=50)
    ap.add_argument("--per-snapshot", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--insecure", action="store_true")
    args = ap.parse_args()

    url, api_key = resolve_credentials(args)
    if not url:
        sys.exit("ERROR: no Elasticsearch endpoint resolved. Use --cluster "
                 "(AWS Secrets Manager), --es-url, or the ES_URL environment variable.")
    es = ESClient(url, api_key=api_key, insecure=args.insecure)

    sys.stderr.write(f"Repo    : {args.repo}\nPattern : {args.pattern}\n")
    sys.stderr.write("Collecting in-use snapshots from mounted indices...\n")
    in_use = collect_in_use(es, args.repo)
    sys.stderr.write(f"  in-use snapshots: {len(in_use)}\n")

    sys.stderr.write(f"Listing all snapshots in {args.repo}...\n")
    all_snaps = list_all_snapshots(es, args.repo)
    sys.stderr.write(f"  total snapshots : {len(all_snaps)}\n")

    orphans = [s for s in all_snaps
               if s not in in_use and fnmatch.fnmatch(s, args.pattern)]
    sys.stderr.write(f"  orphaned (match): {len(orphans)}\n")

    if not orphans:
        report = {"repo": args.repo, "pattern": args.pattern, "orphan_count": 0,
                  "total_bytes": 0, "incremental_bytes": 0}
        print(json.dumps(report, indent=2) if args.json else "No orphaned snapshots found.")
        return

    sys.stderr.write(f"Sizing {len(orphans)} orphan(s) via _status (batch={args.batch})...\n")
    total, incr, per = size_snapshots(es, args.repo, orphans, args.batch)

    report = {
        "repo": args.repo,
        "pattern": args.pattern,
        "orphan_count": len(orphans),
        "measured_count": len(per),
        "total_bytes": total,
        "total_human": human(total),
        "incremental_bytes": incr,
        "incremental_human": human(incr),
    }
    if args.per_snapshot:
        report["per_snapshot"] = [
            {"snapshot": n, "total_bytes": v["total"],
             "incremental_bytes": v["incremental"]}
            for n, v in sorted(per.items(), key=lambda kv: kv[1]["total"], reverse=True)
        ]

    if args.json:
        print(json.dumps(report, indent=2))
        return

    print("\n==================== ORPHANED SNAPSHOT STORAGE ====================")
    print(f"  repository                : {args.repo}")
    print(f"  name pattern              : {args.pattern}")
    print(f"  orphaned snapshots        : {len(orphans)} (measured {len(per)})")
    print(f"  total (logical) size      : {human(total)}   ({total} bytes)")
    print(f"  incremental (reclaimable) : {human(incr)}   ({incr} bytes)")
    print("==================================================================")
    print("  'incremental' is the dedup-aware estimate of space freed by deleting")
    print("  these snapshots. For the exact repository bill, also check the backing")
    print("  object-storage bucket metrics (S3/GCS/Azure).")
    if args.per_snapshot:
        print("\n  Largest orphans (by total size):")
        for row in report["per_snapshot"][:25]:
            print(f"    {human(row['total_bytes']):>12}  {row['snapshot']}")


if __name__ == "__main__":
    main()
