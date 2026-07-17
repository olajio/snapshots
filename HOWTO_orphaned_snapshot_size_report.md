# How-To: `orphaned_snapshot_size_report.py`

A **read-only** helper that reports how much repository storage is being consumed by
**orphaned searchable snapshots** — snapshots left behind in the `found-snapshots`
repository after their frozen index was deleted (or its ILM policy changed) without ILM
running the delete phase.

The script **never deletes anything**. It only measures. To actually remove orphans, use
`cleanup_orphaned_searchable_snapshots.sh`.

---

## 1. What it does

1. Finds the snapshots **currently in use** — those referenced by a mounted
   searchable-snapshot index (via each index's `index.store.snapshot.snapshot_name`
   setting).
2. Lists **every** snapshot in the repository.
3. Computes **orphans** = all snapshots − in-use snapshots (optionally filtered by a
   name pattern).
4. Calls the `_status` API for those orphans and sums two figures:
   - **`total` (logical size)** — the full size of each snapshot. Because snapshots share
     deduplicated blobs, summing this **overcounts** shared data (upper bound).
   - **`incremental` (reclaimable)** — the bytes each snapshot *uniquely* added to the
     repository. This is the **dedup-aware estimate of what you actually free** by
     deleting the orphans. **This is the number to quote for savings.**

---

## 2. Requirements

- **Python 3.7+** — standard library only, nothing to `pip install`.
- Network access to the Elasticsearch endpoint.
- Credentials that can call `_snapshot`, `_settings`, and `_status` (a monitoring/admin
  role, or superuser).

---

## 3. Providing connection details

You can pass the endpoint and credentials **either as command-line arguments or as
environment variables**. CLI arguments win when both are set.

| CLI argument | Environment variable | Meaning |
|--------------|----------------------|---------|
| `--es-url`   | `ES_URL`             | Elasticsearch endpoint, e.g. `https://host:9243` |
| `--api-key`  | `ES_API_KEY`         | API key ("encoded" value) |
| `--es-user`  | `ES_USER`            | Basic-auth username (alternative to API key) |
| `--es-pass`  | `ES_PASS`            | Basic-auth password |

Use **either** an API key **or** a username/password pair.

> **Security note:** secrets passed as CLI arguments can show up in your shell history and
> in the process list (`ps`). For anything beyond a quick test, prefer the environment
> variables, and consider `export HISTCONTROL=ignorespace` (then prefix the command with a
> space) if you must inline a key.

### Getting an API key

In Kibana **Dev Tools**:

```json
POST /_security/api_key
{
  "name": "orphan-snapshot-report",
  "role_descriptors": {
    "snap_reader": {
      "cluster": ["monitor", "read_ilm"],
      "index":   [{ "names": ["*"], "privileges": ["view_index_metadata"] }]
    }
  }
}
```

Use the **`encoded`** value from the response as `--api-key` / `ES_API_KEY`.

---

## 4. Quick start

Pass everything on the command line:

```bash
./orphaned_snapshot_size_report.py \
  --es-url https://my-deployment.es.us-east-1.aws.found.io:9243 \
  --api-key "PASTE_ENCODED_API_KEY"
```

Or use environment variables (unchanged, still supported):

```bash
export ES_URL="https://my-deployment.es.us-east-1.aws.found.io:9243"
export ES_API_KEY="PASTE_ENCODED_API_KEY"
./orphaned_snapshot_size_report.py
```

Example output:

```
==================== ORPHANED SNAPSHOT STORAGE ====================
  repository                : found-snapshots
  name pattern              : *
  orphaned snapshots        : 1420 (measured 1420)
  total (logical) size      : 3.42 TiB   (3761...bytes)
  incremental (reclaimable) : 3.11 TiB   (3420...bytes)
==================================================================
```

> Progress lines ("Collecting in-use snapshots...", "...sized 50/1420") are written to
> **stderr**, so the clean report / JSON on **stdout** can be redirected to a file.

---

## 5. All options

| Option | Default | Purpose |
|--------|---------|---------|
| `--es-url URL` | — | Endpoint (overrides `ES_URL`) |
| `--api-key KEY` | — | API key (overrides `ES_API_KEY`) |
| `--es-user USER` | — | Basic-auth username (overrides `ES_USER`) |
| `--es-pass PASS` | — | Basic-auth password (overrides `ES_PASS`) |
| `--repo NAME` | `found-snapshots` | Snapshot repository to inspect |
| `--pattern GLOB` | `*` | Only size orphans whose name matches this shell glob |
| `--batch N` | `50` | Snapshots per `_status` request |
| `--per-snapshot` | off | Also print the largest orphans individually |
| `--json` | off | Emit machine-readable JSON instead of text |
| `--insecure` | off | Skip TLS verification (not recommended) |
| `-h`, `--help` | — | Show help and exit |

---

## 6. Common recipes

**Scope to a single year** (searchable-snapshot names start with the snapshot date, e.g.
`2023.02.17-...`):

```bash
./orphaned_snapshot_size_report.py --es-url "$URL" --api-key "$KEY" --pattern '2023.*'
```

**See the biggest offenders:**

```bash
./orphaned_snapshot_size_report.py --es-url "$URL" --api-key "$KEY" --per-snapshot
```

**Save a JSON report** (progress goes to stderr, so the file stays clean):

```bash
./orphaned_snapshot_size_report.py --es-url "$URL" --api-key "$KEY" --json > orphan_size.json
```

JSON shape:

```json
{
  "repo": "found-snapshots",
  "pattern": "*",
  "orphan_count": 1420,
  "measured_count": 1420,
  "total_bytes": 3761...,
  "total_human": "3.42 TiB",
  "incremental_bytes": 3420...,
  "incremental_human": "3.11 TiB"
}
```

Add `--per-snapshot` to include a sorted `per_snapshot` array in the JSON.

**Basic-auth instead of an API key:**

```bash
./orphaned_snapshot_size_report.py --es-url "$URL" --es-user elastic --es-pass "$PASSWORD"
```

---

## 7. Interpreting the numbers

- Report **`incremental` (reclaimable)** as the expected space savings — it accounts for
  deduplication between snapshots. For the force-merged searchable snapshots in this
  environment there is usually little sharing, so `total` and `incremental` tend to be
  close.
- For the **exact repository bill**, cross-check the backing object-storage bucket metrics
  (AWS S3 `BucketSizeBytes`, or the GCS/Azure equivalent) for the deployment's repository
  path. That is the ground truth for what you are billed and sidesteps dedup-counting
  entirely.

---

## 8. Performance & safety notes

- The `_status` API is **heavy and blocking** — it reads shard-level stats for each
  snapshot. The script batches requests (default 50). On very large repositories, or if
  you notice contention with your regular SLM snapshots, lower `--batch` (e.g. `--batch 20`)
  and/or scope with `--pattern`.
- The script is **strictly read-only**: it issues only `GET` requests. It cannot delete or
  modify snapshots.

---

## 9. Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| `ERROR: provide the endpoint via --es-url or the ES_URL environment variable` | No endpoint given — pass `--es-url` or export `ES_URL`. |
| `ERROR: set ES_API_KEY, or ES_USER and ES_PASS` | No credentials — pass `--api-key`, or `--es-user`+`--es-pass`. |
| `HTTP 401` / `403` | Credentials invalid or lack privileges — grant `monitor` + `view_index_metadata`. |
| `HTTP 404` on `_snapshot/...` | Wrong repository name — set `--repo` to your actual repo. |
| TLS / certificate errors | Use a proper CA-trusted endpoint; `--insecure` is a last resort for testing only. |
| Runs slowly / affects the cluster | Lower `--batch`, scope with `--pattern`, or run off-peak. |

---

## 10. Related files

- `cleanup_orphaned_searchable_snapshots.sh` — deletes orphans (dry-run by default; has its
  own `--report-size` mode using the same detection logic).
- `analyze_ilm.py` — audits ILM policies for the missing/`false` `delete_searchable_snapshot`
  setting that causes orphans in the first place.
- `searchable_snapshot_ilm_findings.md` — the full audit write-up and background.
