## Summary table

| Cluster | Total | In-use | SLM (excl.) | Orphans | Logical size | **Reclaimable** (dedup-aware) | Dedup ratio | Offending ILM |
|---|--:|--:|--:|--:|--:|--:|--:|---|
| **ccs** | 101 | 0 | 101 | **0** | — | — | — | none |
| **dev** | 507 | 175 | 101 | 231 | 3.30 TiB | **638.77 GiB** | 5.3× | `solarwinds-test` (0 orphans) |
| **qa** | 1,005 | 377 | 101 | 527 | 10.34 TiB | **8.47 TiB** | 1.2× | none |
| **prod** | 6,318 | 4,926 | 101 | 1,291 | 51.44 TiB | **220.47 GiB** | 239× | `cost` (0 orphans) |

SLM exclusion is working everywhere (101 `cloud-snapshot-*` excluded per cluster — that's the ~16.7-day retention). **The reclaimable column is the number that matters, and it tells a very different story per cluster.**

The key insight is the **dedup ratio**: logical size ≠ reclaimable. Look at the per-snapshot `incremental` column — where it's ~0–400 B, the snapshot's data is already held by *another* snapshot (often a live in-use one), so deleting it frees almost nothing.

---

## Per-cluster analysis & next actions

### 🟢 CCS — nothing to do
101 snapshots, all SLM-managed, **0 orphans, 0 in-use searchable snapshots, no offending policies.** CCS is a cross-cluster-search coordinator with no data tiers. **Action: none.** ✅

### 🔴 QA — highest priority (real 8.47 TiB win)
527 orphans, and unlike the others the data is **mostly unique** (1.2× dedup — the 2025 `filebeat-8.11.0-mt-qa` entries show incremental ≈ total, e.g. 44 GiB = 44 GiB). So deleting them genuinely frees **~8.47 TiB** — by far the biggest payoff of any cluster.
- One caution: **396 of the 527 are dated 2026** (recent). They're legitimately unreferenced, but that's a lot of recent orphaning — do a quick human sanity-check that those frozen indices were *meant* to be gone before mass-deleting.
- No policy fix needed (no offending ILM).
- **Action:** review the 2026 batch, then delete all 527.

### 🟡 DEV — moderate (639 GiB), plus a policy to fix
231 orphans, 3.30 TiB logical but **~639 GiB reclaimable** (5.3× dedup — the big metricbeat/filebeat `mt-rnd` snapshots share their blobs). All historical (2023–2024), from now-compliant policies.
- **Offending policy `solarwinds-test`** (frozen searchable snapshot, no delete phase; 0 orphans so far — a latent leak).
- **Action:** delete the 231 orphans; **fix `solarwinds-test`** — the corrected body is already in the repo at `corrected_ilm_policies/solarwinds-test.json` (set a real `min_age` first).

### 🟠 PROD — big headline, small real payoff; be careful
1,291 orphans and **51.44 TiB logical — but only ~220 GiB reclaimable** (239× dedup!). The top entries are 50+ GiB with **0 B incremental** — their data is pinned by the **4,926 in-use** mounted snapshots. Deleting the orphans is **safe** (ES reference-counts blobs, so live indices are untouched) but frees only ~220 GiB.
- **Offending policy `cost`** (frozen searchable snapshot, no delete phase; 0 orphans — latent). Corrected body ready at `corrected_ilm_policies/cost.json`.
- **Action:** fix `cost` (prevents future leaks — higher value than the cleanup here); schedule the orphan deletion for an **off-peak window** (1,291 deletes on a busy prod repo will contend with SLM). Storage urgency is low; hygiene/repo-performance is the reason to do it.

---

## Recommended order

1. **QA** — biggest reclaim (8.47 TiB). Sanity-check the 2026 batch, then delete.
2. **DEV** — delete (639 GiB) + fix `solarwinds-test`.
3. **PROD** — fix `cost` now; delete orphans in a maintenance window (low storage payoff).
4. **CCS** — done.

**Total genuinely reclaimable across all clusters: ~9.3 TiB, of which QA is ~91%.**

Delete the **whole** orphan set per cluster (partial `--pattern` deletes underperform badly on dev/prod because of the shared base blobs). Suggested per cluster, capturing an audit of what was removed:

```bash
# dry-run is what you already have; this deletes and records the run
./orphaned_searchable_snapshots.py --cluster qa --apply \
  --audit-file qa_deleted_$(date +%F).txt 2>&1 | tee qa_delete.log
```

Verify the actual space freed with the **object-storage bucket size before vs. after** (especially prod, where the logical number is wildly higher than reclaimable).

Want me to (a) finalize the `cost` and `solarwinds-test` corrected policies with a concrete `min_age`, and/or (b) draft a short runbook (markdown) in the repo capturing this per-cluster plan and the delete/verify commands?
