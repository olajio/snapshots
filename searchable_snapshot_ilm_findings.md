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
