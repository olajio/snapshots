# Searchable Snapshot Orphan Cleanup

Tooling to find, size, and clean up **orphaned searchable snapshots** in Elasticsearch /
Elastic Cloud deployments, catch the ILM policies that create them, and quantify how much
storage a cleanup would actually reclaim.

---

## Why this project matters (motivation)

Searchable snapshots back the **cold and frozen data tiers** — they let you keep large
amounts of older data queryable at a fraction of the cost of hot storage, because the data
lives in cheap **object storage** (S3/GCS/Azure) instead of on local SSD.

The catch: those snapshots are supposed to be **deleted by ILM** when their index ages out.
When they aren't (a misconfigured policy, a policy edited after the fact, or an index
deleted by hand), the snapshot is **orphaned** — it stays in the repository forever,
serving no index and **quietly accruing object-storage cost**. Across a fleet these
accumulate into terabytes of dead data, and they also:

- **inflate the storage bill** for data nobody can query,
- **slow down snapshot/SLM operations** and repository housekeeping as the snapshot count grows,
- **clutter the snapshot list**, making real backups harder to reason about.

This project makes the problem **measurable and safe to fix**: it identifies exactly which
snapshots are orphaned, how much space they *actually* reclaim (accounting for
deduplication), which ILM policies are the root cause, and — with a capacity figure you
provide — **what percentage of your frozen-tier storage the cleanup would free**, so you can
decide whether it's worth the effort before doing anything destructive.

---

## Concepts (glossary)

- **Data tiers** — hot / warm / cold / frozen. Older, less-queried data is moved to cheaper
  tiers. **Cold** and **frozen** use *searchable snapshots*.
- **Searchable snapshot** — an index whose data lives in a **snapshot** in the repository and
  is queried directly from there. **Frozen** indices are *partially mounted*: the data stays
  in object storage and the frozen nodes keep only a small **local cache**.
- **Snapshot repository** (`found-snapshots`) — the object-storage bucket where all snapshots
  (and thus all frozen-tier data) physically live. This is what you're billed for.
- **ILM (Index Lifecycle Management)** — policies that move an index through phases
  (hot → … → frozen → delete). The `searchable_snapshot` action creates the snapshot; the
  `delete` phase's `delete_searchable_snapshot` option (default **`true`**) removes it.
- **SLM (Snapshot Lifecycle Management)** — separate, scheduled *backups* (e.g. the periodic
  `cloud-snapshot-*` snapshots). SLM manages its own retention, so **SLM snapshots are never
  orphans**.
- **Orphaned searchable snapshot** — a searchable snapshot that no live index references and
  that ILM never deleted. See the exact definition below.
- **Logical vs. incremental (reclaimable) size** — snapshots are **deduplicated**: they share
  underlying blobs. A snapshot's **logical** size counts its full contents (over-counts
  shared data); its **incremental** size is what it *uniquely* added — i.e. the space
  actually freed if you delete it. **Reclaimable = incremental.**

### What counts as an "orphan"

A snapshot is an orphan when **all** of these hold:

- it lives in the snapshot repository, **and**
- it is **not referenced by any mounted** searchable-snapshot index (its index is gone), **and**
- it is **not managed by SLM** (no `metadata.policy`) — SLM retires its own snapshots.

---

## What's in this repo

| Path | Purpose |
|------|---------|
| `orphaned_searchable_snapshots.py` | The main tool — find, size, delete orphans, flag culprit ILM policies, and estimate reclaimable storage, against a live cluster. |
| `HOWTO_orphaned_searchable_snapshots.md` | Detailed usage guide (options, recipes, troubleshooting). |
| `analyze_ilm.py` | Offline auditor — parses exported `GET _ilm/policy` JSON files and flags leaking policies. |
| `corrected_ilm_policies/` | Ready-to-apply `PUT _ilm/policy` bodies that add a delete phase to the leaking policies. |
| `searchable_snapshot_ilm_findings.md` | The full audit write-up and background. |
| `dev_ilm_policy`, `qa_ilm_policy`, `prod_ilm_policy`, `ccs_ilm_policy` | Exported ILM policy snapshots per cluster (input to `analyze_ilm.py`). |

---

## Quick start

The tool is **read-only and dry-run by default** — it never deletes anything unless you
pass `--apply`.

```bash
# List orphans for a cluster (credentials pulled from AWS Secrets Manager)
./orphaned_searchable_snapshots.py --cluster qa

# List orphans + their storage + the culprit ILM policies, in one pass
./orphaned_searchable_snapshots.py --cluster qa --report-size --check-ilm
```

`--cluster {dev,qa,ccs,prod}` loads the endpoint and API key from AWS Secrets Manager secret
`elastic/kibana/dataview_cleanup_<cluster>` (keys `es_url` and `es_api_key`). Authentication
is **API key only**.

---

## Key capabilities

- **Credentials from AWS Secrets Manager** (`--cluster`) — nothing secret on the command
  line; boto3 if available, else the `aws` CLI.
- **Fast, timeout-safe sizing** (`--report-size`) via the Get Snapshot `index_details`
  metadata; `--incremental` for the dedup-aware **reclaimable** figure via `_status`.
- **ILM culprit analysis** (`--check-ilm`) — flags policies that create searchable snapshots
  but won't let ILM delete them, with the count/size of orphans each has produced. It also
  **writes a ready-to-apply corrected policy** for each culprit to
  `corrected_ilm_policies/<cluster>/<policy>.json` (adds/fixes `delete_searchable_snapshot`).
- **ILM review file** (`--ilm-review-file`) — one file covering **both** the currently
  offending policies **and** policies compliant *now* that leaked in the past (with
  last-updated date); flags any that leaked *after* their last update as **NEEDS REVIEW**.
- **Frozen-tier share** — two related "is it worth it?" views:
  - `--frozen-usage`: orphans as a % of the frozen tier's **logical** searchable-snapshot
    storage (also sizes the in-use mounted snapshots; computed from ES metadata).
  - `--frozen-tier-capacity`: **reclaimable** orphan storage as a % of the **total
    frozen-tier object-store storage** you provide (see below).
- **SLM-aware** — excludes SLM-managed snapshots from the orphan set.
- **Audit records** (`--audit-file`) — full orphan list + summary/analysis to a text file
  (screen still shows the top 25).
- **Safe deletion** (`--apply`) — dry-run by default; requests batched under the ES HTTP
  request-line limit and retried with backoff on timeouts / `429` / `5xx`.

---

## Reclaimable as a % of frozen-tier storage

`incremental` tells you the **bytes** a cleanup frees. To decide if it's *worth it*, you
usually want that as a **share of your total frozen-tier storage**. That total lives in the
**object-store bucket** and **cannot be read from any Elasticsearch API** (node stats only
report each frozen node's small local cache disk, not the object store). So you supply it —
read it from the **Elastic Cloud console** — and the tool reports:

> reclaimable (`incremental`) ÷ your provided capacity × 100

```bash
# provide the capacity inline (units: GiB or TiB)
./orphaned_searchable_snapshots.py --cluster qa --frozen-tier-capacity 60TiB

# or be prompted for it at runtime (pass the flag with no value)
./orphaned_searchable_snapshots.py --cluster qa --frozen-tier-capacity
```

`--frozen-tier-capacity` implies `--incremental` (the numerator must be the reclaimable
figure). Sizes use binary units (1 TiB = 1024⁴ bytes), matching the Cloud console.
(`--frozen-usage` answers the related but different question of the orphans' **logical**
share and needs no capacity input.)

If you run with **`--incremental` but omit `--frozen-tier-capacity`**, the tool **prompts**
you for the capacity so you still get the reclaimable-% report — press Enter to skip it. (In
a piped/non-interactive run there's no prompt; pass the value inline instead.)

---

## Sample commands (all options)

```bash
# ---- discovery (read-only) ----
# dry-run: list orphans only
./orphaned_searchable_snapshots.py --cluster dev

# fast size (logical) of the orphans
./orphaned_searchable_snapshots.py --cluster dev --report-size

# dedup-aware reclaimable size (slower _status scan; tune batch/timeout on big repos)
./orphaned_searchable_snapshots.py --cluster dev --incremental --batch 20 --timeout 300

# largest 25 orphans with sizes
./orphaned_searchable_snapshots.py --cluster dev --per-snapshot

# scope to a year
./orphaned_searchable_snapshots.py --cluster dev --pattern '2023.*' --report-size

# ---- ILM analysis ----
# flag policies that will keep creating orphans
./orphaned_searchable_snapshots.py --cluster dev --check-ilm

# log now-compliant policies that leaked before (flags post-update re-leaks)
./orphaned_searchable_snapshots.py --cluster dev --report-size \
  --ilm-review-file dev_ilm_review.txt

# ---- worth-it decision ----
# orphans as a % of frozen-tier LOGICAL searchable-snapshot storage
./orphaned_searchable_snapshots.py --cluster dev --frozen-usage

# reclaimable as a % of the frozen-tier OBJECT-STORE capacity (from Cloud console)
./orphaned_searchable_snapshots.py --cluster dev --frozen-tier-capacity 60TiB

# ---- everything in one audited pass ----
./orphaned_searchable_snapshots.py --cluster dev \
  --incremental --check-ilm \
  --frozen-tier-capacity 60TiB \
  --ilm-review-file dev_ilm_review.txt \
  --audit-file dev_orphans_audit.txt 2>&1 | tee dev_run.log

# ---- everything in one audited pass. Sample usage ----
./orphaned_searchable_snapshots.py --cluster dev \
  --incremental --check-ilm --report-size \
  --frozen-tier-capacity 60TiB \
  --ilm-review-file dev_ilm_review.txt \
  --audit-file dev_orphans_audit.txt 2>&1 | tee dev_run.log

# ---- credentials without --cluster ----
./orphaned_searchable_snapshots.py --es-url https://host:9243 --api-key "$KEY" --report-size
ES_URL=... ES_API_KEY=... ./orphaned_searchable_snapshots.py --report-size
# custom AWS secret name / region
./orphaned_searchable_snapshots.py --secret-name my/secret --region us-east-1 --report-size

# ---- machine-readable output ----
./orphaned_searchable_snapshots.py --cluster dev --report-size --check-ilm --json > dev.json

# ---- deletion (destructive; review the dry-run first) ----
# delete ALL orphans (recommended: whole set, since partial deletes underperform)
./orphaned_searchable_snapshots.py --cluster dev --apply --audit-file dev_deleted.txt
# delete only 2023 orphans
./orphaned_searchable_snapshots.py --cluster dev --pattern '2023.*' --apply
```

Run `./orphaned_searchable_snapshots.py --help` for the complete flag list.

---

## Required API-key privileges

- Reporting / listing / sizing: `monitor`, `view_index_metadata`
- `--check-ilm` / `--ilm-review-file`: `read_ilm`
- `--apply` (delete): `manage` / `cluster:admin/snapshot/delete`

---

## Interpreting the numbers (important)

- **`total (logical)`** over-counts blobs shared between snapshots — an upper bound.
- **`incremental` (reclaimable)** is the dedup-aware estimate of space actually freed. On
  heavily-shared data these differ a lot (e.g. a repo showing 51 TiB logical may only reclaim
  ~220 GiB because most blobs are pinned by live snapshots).
- The **exact** freed space is the object-store bucket size before vs. after deletion.
- To realize the full reclaim, delete the **whole** orphan set — partial (`--pattern`)
  deletes can free far less when a shared "base" snapshot stays behind.

---

## Fixing the root cause

Deleting orphans is only half the job — leaking ILM policies keep making new ones. Running
with `--check-ilm` **auto-generates a corrected `PUT _ilm/policy` body** for every culprit at
`corrected_ilm_policies/<cluster>/<policy>.json`:

- a delete phase that sets `delete_searchable_snapshot: false` → flipped to `true`;
- a policy with **no** delete phase → a delete phase is added with
  `delete_searchable_snapshot: true` and a **placeholder `min_age` of `365d`** — **review
  that retention against your needs before applying.**

Apply one with `PUT _ilm/policy/<policy>` using the generated JSON as the body.

---

## Full documentation

- **Usage guide:** [`HOWTO_orphaned_searchable_snapshots.md`](HOWTO_orphaned_searchable_snapshots.md)
- **Audit & background:** [`searchable_snapshot_ilm_findings.md`](searchable_snapshot_ilm_findings.md)
