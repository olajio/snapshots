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

Connection (pass as CLI arguments OR environment variables)
------------------------------------------------------------
  --es-url URL     (or ES_URL)      e.g. https://my-deployment.es.us-east-1.aws.found.io:9243
  --api-key KEY    (or ES_API_KEY)  an API key ("encoded" value)          -- OR --
  --es-user USER   (or ES_USER)     basic-auth username
  --es-pass PASS   (or ES_PASS)     basic-auth password

  CLI arguments take precedence over the environment variables when both are set.
  NOTE: passing secrets as CLI arguments can expose them in shell history and the
  process list (ps). Environment variables are safer for the API key/password.

Usage
-----
  # everything on the command line
  ./orphaned_snapshot_size_report.py --es-url https://host:9243 --api-key "$KEY"
  ./orphaned_snapshot_size_report.py --es-url https://host:9243 --api-key "$KEY" --pattern '2023.*'
  ./orphaned_snapshot_size_report.py --es-url https://host:9243 --api-key "$KEY" --json > report.json

  # or via environment variables (unchanged, still supported)
  ES_URL=... ES_API_KEY=... ./orphaned_snapshot_size_report.py

Options
-------
  --es-url URL      Elasticsearch endpoint (overrides ES_URL)
  --api-key KEY     API key, "encoded" value (overrides ES_API_KEY)
  --es-user USER    Basic-auth username (overrides ES_USER)
  --es-pass PASS    Basic-auth password (overrides ES_PASS)
  --repo NAME       Snapshot repository (default: found-snapshots)
  --pattern GLOB    Only size orphans whose name matches this glob (default: '*')
  --batch N         Snapshots per _status request (default: 50)
  --per-snapshot    Also print a per-snapshot size breakdown (largest first)
  --json            Emit the report as JSON instead of text
  --insecure        Skip TLS verification (not recommended)

Requires: Python 3.7+ (standard library only).
"""

import argparse
import base64
import fnmatch
import json
import os
import ssl
import sys
import urllib.error
import urllib.request


def human(n):
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if n < 1024 or unit == "PiB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


class ESClient:
    def __init__(self, url, api_key=None, user=None, password=None, insecure=False):
        self.base = url.rstrip("/")
        self.headers = {"Content-Type": "application/json"}
        if api_key:
            self.headers["Authorization"] = f"ApiKey {api_key}"
        elif user and password:
            token = base64.b64encode(f"{user}:{password}".encode()).decode()
            self.headers["Authorization"] = f"Basic {token}"
        else:
            sys.exit("ERROR: set ES_API_KEY, or ES_USER and ES_PASS")
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


def main():
    ap = argparse.ArgumentParser(description="Report storage used by orphaned searchable snapshots.")
    ap.add_argument("--es-url", help="Elasticsearch endpoint (overrides ES_URL)")
    ap.add_argument("--api-key", help="API key, 'encoded' value (overrides ES_API_KEY)")
    ap.add_argument("--es-user", help="Basic-auth username (overrides ES_USER)")
    ap.add_argument("--es-pass", help="Basic-auth password (overrides ES_PASS)")
    ap.add_argument("--repo", default="found-snapshots")
    ap.add_argument("--pattern", default="*")
    ap.add_argument("--batch", type=int, default=50)
    ap.add_argument("--per-snapshot", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--insecure", action="store_true")
    args = ap.parse_args()

    # CLI arguments take precedence over environment variables.
    url = args.es_url or os.environ.get("ES_URL")
    api_key = args.api_key or os.environ.get("ES_API_KEY")
    user = args.es_user or os.environ.get("ES_USER")
    password = args.es_pass or os.environ.get("ES_PASS")

    if not url:
        sys.exit("ERROR: provide the endpoint via --es-url or the ES_URL environment variable")
    es = ESClient(
        url,
        api_key=api_key,
        user=user,
        password=password,
        insecure=args.insecure,
    )

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
