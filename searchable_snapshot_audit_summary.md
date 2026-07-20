# Searchable Snapshot Orphan Audit — Summary (2026-07-20)

**Scope:** orphaned searchable snapshots in the `found-snapshots` repo across all 4 clusters.
An *orphan* = a searchable snapshot no live (mounted) index references and that ILM never
deleted. SLM `cloud-snapshot-*` backups are excluded (SLM manages their retention).

| Cluster | Orphans | Logical size | Reclaimable* | Currently-leaking ILM | Policies to review |
|---------|--------:|-------------:|-------------:|-----------------------|-------------------:|
| **PROD** | 1,291 | 51.44 TiB | **~220 GiB** | `cost` (0 orphans yet) | 2 |
| **QA**   | 527   | 10.34 TiB | **~8.47 TiB** | none | 6 |
| **DEV**  | 231   | 3.30 TiB  | **~639 GiB** | `solarwinds-test` (0 orphans yet) | 14 |
| **CCS**  | 0     | — | — | none | 0 |

*Reclaimable = dedup-aware (`incremental`); logical over-counts blobs shared between
snapshots. **Total reclaimable ≈ 9.3 TiB, ~91% of it in QA.** Confirm via the repo's
object-storage bucket size before/after deletion.

## Key finding (for Elastic)
`apm-rollover-30-days` is **still producing orphaned APM frozen snapshots after its last
policy update** on QA and DEV — i.e. the snapshot's index is being removed outside ILM's
delete phase, or ILM isn't deleting the searchable snapshot:
- **QA:** 405 orphans / 8.38 TiB logical, latest **2026-02-04** (policy last updated 2026-01-07) — ~99% of QA's reclaimable data.
- **DEV:** 127 orphans / 2.00 TiB logical, latest **2026-02-04** (policy last updated 2025-08-22).

## Policies currently misconfigured (no delete phase → will leak going forward)
- **PROD:** `cost` · **DEV:** `solarwinds-test` — both 0 orphans so far (latent).
  Fix: add a delete phase with `delete_searchable_snapshot: true`.

## Recommended actions
1. **Elastic:** advise why `apm-rollover-30-days` frozen snapshots orphan after the policy update on QA+DEV (post-update, still 2026 dates).
2. **Clean up orphans** (dry-run verified, safe — live indices unaffected): **QA first** (~8.47 TiB), then **DEV** (~639 GiB), **PROD** in a maintenance window (~220 GiB, low payoff, production).
3. **Fix now:** `cost` (PROD) and `solarwinds-test` (DEV) policies, to stop future leaks.
4. **CCS:** no action — clean.

_Method: audited with `orphaned_searchable_snapshots.py` (`--report-size --incremental --check-ilm --ilm-review-file`). Full per-snapshot lists and per-policy review in the attached `*_orphans_audit.txt` / `*_ilm_review.txt`._
