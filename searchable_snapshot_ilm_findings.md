# Searchable Snapshot ILM Audit

Context: Elastic support case on old searchable snapshots (some from 2023) not being
removed. In ILM, `delete_searchable_snapshot` is an option of the `delete` action and
**defaults to `true`**. A policy therefore leaks searchable snapshots only when it takes
a searchable snapshot but (a) has no `delete` phase, or (b) explicitly sets
`delete_searchable_snapshot: false`.

`analyze_ilm.py` parses the four `GET _ilm/policy` exports and checks every policy.

## Results

| Cluster | Total policies | Use searchable snapshots | Explicit `false` | Missing delete phase (will leak) |
|---------|---------------:|-------------------------:|-----------------:|----------------------------------|
| DEV     | 155            | 101                      | 0                | 1 -> `solarwinds-test`           |
| QA      | 150            | 83                       | 0                | 0                                |
| PROD    | 186            | 116                      | 0                | 1 -> `cost`                      |
| CCS     | 65             | 0                        | 0                | 0                                |

No policy in any cluster sets `delete_searchable_snapshot: false`; every explicit
occurrence is `true`.

## Policies that will orphan searchable snapshots

Both take a searchable snapshot in the frozen phase but have **no delete phase**, so the
snapshot in `found-snapshots` is never removed by ILM:

- **DEV -> `solarwinds-test`** (modified 2025-08-04): `hot -> frozen(searchable_snapshot)`, no delete phase.
- **PROD -> `cost`** (modified 2025-09-15): `hot -> frozen(searchable_snapshot)`, no delete phase.

QA and CCS are clean.

## Note on the `metricbeat-7.13.4` example from the case

That policy lives in the QA cluster (updated 2023-06-21T13:14:53.420Z) and its current
config is correct (cold + frozen searchable snapshots WITH a delete phase,
`delete_searchable_snapshot` at its `true` default). The leftover 2023 snapshots are
historical orphans (policy edited or frozen indices deleted after the fact), not a
present-day misconfiguration. They require manual snapshot cleanup, e.g.:

    DELETE _snapshot/found-snapshots/<searchable_snapshot_name>
    # or wildcard for a year:
    DELETE _snapshot/found-snapshots/found-snapshots/2023.*

## Recommended actions

1. Fix forward: add a `delete` phase (with `delete_searchable_snapshot: true`) to
   `solarwinds-test` (DEV) and `cost` (PROD).
2. Clean up the backlog: manually delete the already-orphaned snapshots; no policy change
   removes snapshots already detached from a live index.

## Deliverables in this branch

- `corrected_ilm_policies/solarwinds-test.json` and `corrected_ilm_policies/cost.json` --
  ready-to-apply `PUT _ilm/policy` bodies. They keep each policy's existing hot/frozen
  phases and add a `delete` phase with `delete_searchable_snapshot: true`.
  - Apply with (adjust host/auth):
    - DEV:  `PUT _ilm/policy/solarwinds-test`  (body = solarwinds-test.json)
    - PROD: `PUT _ilm/policy/cost`             (body = cost.json)
  - **IMPORTANT:** the delete phase `min_age` is set to a placeholder of `365d`. `min_age`
    is measured from index rollover and controls when the index (and its searchable
    snapshot) is deleted. Set this to your actual retention requirement before applying --
    too small a value deletes data early.

- `cleanup_orphaned_searchable_snapshots.sh` -- finds/deletes searchable snapshots in
  `found-snapshots` that are no longer referenced by any mounted index. DRY-RUN by
  default; pass `--apply` to delete, `--pattern '2023.*'` to scope by name, and
  `--report-size` to report how much repository storage the orphans occupy (read-only).

- `orphaned_snapshot_size_report.py` -- standalone, read-only report of the total
  repository storage occupied by ONLY the orphaned searchable snapshots. Sums both the
  logical (`total`) and dedup-aware (`incremental`, i.e. reclaimable) sizes via the
  `_status` API. Supports `--pattern`, `--per-snapshot`, and `--json`.

- `HOWTO_orphaned_snapshot_size_report.md` -- usage guide for the size-report script.

### Authentication (API key only)

Both tools authenticate with an Elasticsearch **API key** (no basic auth). The
recommended path is `--cluster <dev|qa|ccs|prod>`, which loads `es_url` and `es_api_key`
from AWS Secrets Manager secret `elastic/kibana/dataview_cleanup_<cluster>` (the same
secret family as the Kibana data-view cleanup project). Credential resolution order is:
explicit `--es-url`/`--api-key` flags, then the AWS secret, then the `ES_URL`/`ES_API_KEY`
environment variables. AWS lookups use boto3 if available, else the `aws` CLI.

## Measuring how much storage the orphans use

Snapshots are incremental/deduplicated, so summing per-snapshot sizes naively overcounts
shared blobs. Use these instead:

- `incremental.size_in_bytes` (per snapshot, from `_status`) = dedup-aware estimate of what
  deleting that snapshot actually frees. Both tools above report this.
- For the exact repository bill, check the backing object-storage bucket metrics
  (S3 `BucketSizeBytes`, or the GCS/Azure equivalent) for the deployment's repo path.
