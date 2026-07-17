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

- **Python 3.7+.**
- **For `--cluster` (recommended):** either **boto3** installed, or the **`aws` CLI**
  available, plus AWS credentials with `secretsmanager:GetSecretValue` on the relevant
  secret. The script prefers boto3 and falls back to the `aws` CLI automatically.
- Network access to the Elasticsearch endpoint.
- An API key that can call `_snapshot`, `_settings`, and `_status` (a monitoring role is
  enough — `cluster: [monitor]` + `view_index_metadata`).

> Authentication is **API key only** — basic auth (`--es-user`/`--es-pass`) is not
> supported.

---

## 3. Providing connection details

The script needs an **Elasticsearch endpoint** (`es_url`) and an **API key**
(`es_api_key`). It resolves each of them in this order (first match wins):

1. Explicit `--es-url` / `--api-key` flags
2. **AWS Secrets Manager** (when `--cluster` or `--secret-name` is given)
3. Environment variables `ES_URL` / `ES_API_KEY`

### Recommended: AWS Secrets Manager via `--cluster`

Credentials live in AWS Secrets Manager (the same secret family used by the Kibana
duplicate data-view cleanup project). `--cluster <name>` maps to the secret name and pulls
two keys out of it:

| `--cluster` | AWS secret name | keys used |
|-------------|-----------------|-----------|
| `dev`  | `elastic/kibana/dataview_cleanup_dev`  | `es_url`, `es_api_key` |
| `qa`   | `elastic/kibana/dataview_cleanup_qa`   | `es_url`, `es_api_key` |
| `ccs`  | `elastic/kibana/dataview_cleanup_ccs`  | `es_url`, `es_api_key` |
| `prod` | `elastic/kibana/dataview_cleanup_prod` | `es_url`, `es_api_key` |

The secret must be a JSON document containing at least:

```json
{
  "es_url": "https://my-deployment.es.us-east-1.aws.found.io:9243",
  "es_api_key": "PASTE_ENCODED_API_KEY"
}
```

Other keys in the secret (e.g. `kibana_url`) are ignored. This keeps the endpoint and API
key **out of your shell history, environment, and the process list** — the script reads
them straight from Secrets Manager at runtime.

Use `--secret-name` to point at a differently-named secret, and `--region` if the secret
is not in your default AWS region.

### Getting an API key (to store in the secret)

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

Store the **`encoded`** value from the response as `es_api_key` in the cluster's secret.

---

## 4. Quick start

Load the endpoint and API key from AWS Secrets Manager for a cluster (recommended):

```bash
./orphaned_snapshot_size_report.py --cluster prod
./orphaned_snapshot_size_report.py --cluster dev --pattern '2023.*'
```

Or supply them directly / via environment variables:

```bash
# explicit flags
./orphaned_snapshot_size_report.py \
  --es-url https://my-deployment.es.us-east-1.aws.found.io:9243 \
  --api-key "PASTE_ENCODED_API_KEY"

# environment variables
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
| `--cluster {dev,qa,ccs,prod}` | — | Load `es_url`/`es_api_key` from AWS secret `elastic/kibana/dataview_cleanup_<cluster>` |
| `--secret-name NAME` | — | Override the derived AWS secret name |
| `--region NAME` | — | AWS region for Secrets Manager (else default chain) |
| `--es-url URL` | — | Endpoint (overrides secret and `ES_URL`) |
| `--api-key KEY` | — | API key (overrides secret and `ES_API_KEY`) |
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
./orphaned_snapshot_size_report.py --cluster prod --pattern '2023.*'
```

**See the biggest offenders:**

```bash
./orphaned_snapshot_size_report.py --cluster prod --per-snapshot
```

**Save a JSON report** (progress goes to stderr, so the file stays clean):

```bash
./orphaned_snapshot_size_report.py --cluster prod --json > orphan_size.json
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

**Secret in a non-default region, or with a custom name:**

```bash
./orphaned_snapshot_size_report.py --cluster prod --region us-east-1
./orphaned_snapshot_size_report.py --secret-name elastic/kibana/dataview_cleanup_prod
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
| `ERROR: no Elasticsearch endpoint resolved...` | No endpoint — pass `--cluster`, `--es-url`, or export `ES_URL`. |
| `ERROR: no API key resolved...` | No API key — pass `--cluster`, `--api-key`, or export `ES_API_KEY`. |
| `ERROR: AWS secret '...' is missing key(s): es_api_key` | The secret lacks `es_url`/`es_api_key` — add both keys to the secret JSON. |
| `ERROR: reading AWS Secrets Manager needs either boto3 or the aws CLI` | Install boto3 (`pip install boto3`) or the `aws` CLI. |
| `ERROR: failed to read AWS secret '...'` | AWS creds/permissions or region — check `secretsmanager:GetSecretValue` and `--region`. |
| `HTTP 401` / `403` | API key invalid or lacks privileges — grant `monitor` + `view_index_metadata`. |
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
