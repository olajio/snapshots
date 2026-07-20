# Searchable Snapshot Orphan Cleanup

Tooling to find, size, and clean up **orphaned searchable snapshots** in Elasticsearch /
Elastic Cloud deployments, and to catch the ILM policies that create them.

## Background

When an index moves to the cold/frozen tier via ILM, ILM takes a **searchable snapshot**
in the snapshot repository (`found-snapshots`). ILM is supposed to delete that snapshot in
its **delete** phase via `delete_searchable_snapshot: true` (the default). If a policy
takes a searchable snapshot but has **no delete phase**, or sets
`delete_searchable_snapshot: false`, the snapshot is left behind when its index is removed
— an **orphan** that keeps costing object-storage money forever. (See the Elastic support
case summarised in `searchable_snapshot_ilm_findings.md`.)

## What counts as an "orphan"

A snapshot is an orphan when **all** of these hold:

- it lives in the snapshot repository, **and**
- it is **not referenced by any mounted** searchable-snapshot index (its index is gone), **and**
- it is **not managed by SLM** — Snapshot Lifecycle Management stamps `metadata.policy` on
  snapshots it creates (e.g. the periodic `cloud-snapshot-*` backups), and SLM retires
  those on its own retention schedule, so they are **never** orphans.

## What's in this repo

| Path | Purpose |
|------|---------|
| `orphaned_searchable_snapshots.py` | The main tool — find, size, delete orphans, and flag culprit ILM policies, against a live cluster. |
| `HOWTO_orphaned_searchable_snapshots.md` | Detailed usage guide for the tool (options, recipes, troubleshooting). |
| `analyze_ilm.py` | Offline auditor — parses exported `GET _ilm/policy` JSON files and flags leaking policies. |
| `corrected_ilm_policies/` | Ready-to-apply `PUT _ilm/policy` bodies that add a delete phase to the leaking policies. |
| `searchable_snapshot_ilm_findings.md` | The full audit write-up and background. |
| `dev_ilm_policy`, `qa_ilm_policy`, `prod_ilm_policy`, `ccs_ilm_policy` | Exported ILM policy snapshots per cluster (input to `analyze_ilm.py`). |

## Quick start

The tool is **read-only and dry-run by default** — it never deletes anything unless you
pass `--apply`.

```bash
# List orphans for a cluster (credentials pulled from AWS Secrets Manager)
./orphaned_searchable_snapshots.py --cluster qa

# List orphans + their storage + the culprit ILM policies, in one pass
./orphaned_searchable_snapshots.py --cluster qa --report-size --check-ilm

# Show the largest 25 orphans with sizes
./orphaned_searchable_snapshots.py --cluster qa --per-snapshot

# Delete only the 2023 orphans (review first without --apply!)
./orphaned_searchable_snapshots.py --cluster qa --pattern '2023.*' --apply
```

`--cluster {dev,qa,ccs,prod}` loads the endpoint and API key from AWS Secrets Manager
secret `elastic/kibana/dataview_cleanup_<cluster>` (keys `es_url` and `es_api_key`).
Authentication is **API key only**. See the HOWTO for supplying credentials via flags or
environment variables instead.

## Key capabilities

- **Credentials from AWS Secrets Manager** (`--cluster`) — nothing secret on the command
  line; boto3 if available, else the `aws` CLI.
- **Fast, timeout-safe sizing** (`--report-size`) via the Get Snapshot `index_details`
  metadata; `--incremental` for the dedup-aware reclaimable figure via `_status`.
- **ILM culprit analysis** (`--check-ilm`) — flags policies that create searchable
  snapshots but won't let ILM delete them, with the count/size of orphans each has produced.
- **SLM-aware** — excludes SLM-managed snapshots from the orphan set.
- **Safe deletion** (`--apply`) — dry-run by default; requests are batched under the ES HTTP
  request-line limit and retry with backoff on timeouts / `429` / `5xx`.
- **Audit records** (`--audit-file PATH`) — writes the full orphan list plus summary and
  analysis to a text file while the screen still shows only the top 25.

## Required API-key privileges

- Reporting / listing: `monitor`, `view_index_metadata`
- `--check-ilm`: `read_ilm`
- `--apply` (delete): `manage` / `cluster:admin/snapshot/delete`

## Fixing the root cause

Finding and deleting orphans is only half the job — the leaking ILM policies will keep
creating new ones. Use `--check-ilm` to identify them, then apply a corrected policy (add a
delete phase with `delete_searchable_snapshot: true`). Two ready-made examples live in
`corrected_ilm_policies/`; review the delete-phase `min_age` against your retention needs
before applying.

## Full documentation

- **Usage guide:** [`HOWTO_orphaned_searchable_snapshots.md`](HOWTO_orphaned_searchable_snapshots.md)
- **Audit & background:** [`searchable_snapshot_ilm_findings.md`](searchable_snapshot_ilm_findings.md)
