# How-To: `orphaned_searchable_snapshots.py`

One Python tool to **find, size, and (optionally) delete** orphaned searchable
snapshots — snapshots left behind in the `found-snapshots` repository after their
frozen index was deleted (or its ILM policy changed) without ILM running the delete
phase.

It is **dry-run by default** (lists orphans, changes nothing). Add `--report-size` to
measure their storage, and `--apply` to delete them.

> This tool replaces the earlier split `orphaned_snapshot_size_report.py` +
> `cleanup_orphaned_searchable_snapshots.sh`. Everything is now one Python program.

---

## 1. What it does

1. Finds the snapshots **currently in use** — referenced by a mounted searchable-snapshot
   index (via each index's `index.store.snapshot.snapshot_name` setting).
2. Lists **every** snapshot in the repository.
3. Computes **orphans** = all snapshots − in-use snapshots (optionally filtered by a name
   pattern).
4. `--report-size`: calls the `_status` API and sums two figures:
   - **`total` (logical size)** — the full size of each snapshot. Because snapshots share
     deduplicated blobs, summing this **overcounts** shared data (upper bound).
   - **`incremental` (reclaimable)** — bytes each snapshot *uniquely* added; the
     **dedup-aware estimate of what you actually free** by deleting the orphans.
5. `--apply`: deletes the orphans.

### Request batching (why the tool no longer errors with HTTP 400)

Snapshot names are long (~85 chars) and go into the request **URL** for both `_status`
and `DELETE`. Elasticsearch caps the HTTP request line at `http.max_initial_line_length`
(default **4 KB / 4096 bytes**). A naive batch of 50 names produced a ~4098-byte URL and
failed with `too_long_http_line_exception`. The tool now splits work into batches whose
URL stays under a safe **3500-byte** budget (and at most `--batch` names), so it never
trips that limit.

---

## 2. Requirements

- **Python 3.7+.**
- **For `--cluster`:** either **boto3** installed, or the **`aws` CLI** available, plus AWS
  credentials with `secretsmanager:GetSecretValue` on the relevant secret. The tool prefers
  boto3 and falls back to the `aws` CLI automatically.
- Network access to the Elasticsearch endpoint.
- An API key that can call `_snapshot`, `_settings`, and `_status`. Deleting additionally
  needs `manage`/`cluster:admin/snapshot/delete` privileges.

> Authentication is **API key only** — basic auth is not supported.

---

## 3. Providing connection details

The tool needs an **Elasticsearch endpoint** (`es_url`) and an **API key** (`es_api_key`),
resolved in this order (first match wins):

1. Explicit `--es-url` / `--api-key` flags
2. **AWS Secrets Manager** (when `--cluster` or `--secret-name` is given)
3. Environment variables `ES_URL` / `ES_API_KEY`

### Recommended: AWS Secrets Manager via `--cluster`

`--cluster <name>` maps to a secret and reads two keys from it:

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

Other keys (e.g. `kibana_url`) are ignored. This keeps the endpoint and API key **out of
your shell history, environment, and the process list**.

Use `--secret-name` to point at a differently-named secret, and `--region` if it is not in
your default AWS region.

### Getting an API key (to store in the secret)

In Kibana **Dev Tools**:

```json
POST /_security/api_key
{
  "name": "orphan-snapshot-tool",
  "role_descriptors": {
    "snap_admin": {
      "cluster": ["monitor", "manage"],
      "index":   [{ "names": ["*"], "privileges": ["view_index_metadata"] }]
    }
  }
}
```

Store the **`encoded`** value as `es_api_key` in the cluster's secret. (For read-only
reporting, `cluster: [monitor]` alone is enough; `manage` is needed for `--apply`.)

---

## 4. Quick start

```bash
# 1) DRY-RUN: list the orphans for a cluster (creds from AWS Secrets Manager)
./orphaned_searchable_snapshots.py --cluster prod

# 2) List + report how much storage they occupy (read-only)
./orphaned_searchable_snapshots.py --cluster prod --report-size

# 3) Delete only the 2023 orphans (add --apply to actually delete)
./orphaned_searchable_snapshots.py --cluster prod --pattern '2023.*' --apply
```

Or supply credentials directly / via environment variables:

```bash
./orphaned_searchable_snapshots.py --es-url https://host:9243 --api-key "$KEY"
ES_URL=... ES_API_KEY=... ./orphaned_searchable_snapshots.py
```

Example `--report-size` output:

```
==================== ORPHANED SEARCHABLE SNAPSHOTS ====================
  repository         : found-snapshots
  name pattern       : *
  orphaned snapshots : 64
  total (logical)    : 3.42 TiB   (3761...bytes)
  incremental (free) : 3.11 TiB   (3420...bytes)
======================================================================
  ...
  DRY-RUN: nothing deleted. Re-run with --apply to delete the above.
```

> Progress and the per-orphan list go to **stderr**; the final report / JSON goes to
> **stdout**, so `--json > report.json` produces a clean file.

---

## 5. All options

| Option | Default | Purpose |
|--------|---------|---------|
| `--cluster {dev,qa,ccs,prod}` | — | Load `es_url`/`es_api_key` from AWS secret `elastic/kibana/dataview_cleanup_<cluster>` |
| `--secret-name NAME` | — | Override the derived AWS secret name |
| `--region NAME` | — | AWS region for Secrets Manager (else default chain) |
| `--es-url URL` | — | Endpoint (overrides secret and `ES_URL`) |
| `--api-key KEY` | — | API key (overrides secret and `ES_API_KEY`) |
| `--repo NAME` | `found-snapshots` | Snapshot repository |
| `--pattern GLOB` | `*` | Only act on orphans matching this shell glob |
| `--report-size` | off | Report storage used by the orphans (read-only, heavy) |
| `--apply` | off | **Delete** the orphans (without it, dry-run) |
| `--batch N` | `50` | Max snapshots per request (also bounded by URL length) |
| `--per-snapshot` | off | With `--report-size`, print the largest orphans individually |
| `--json` | off | Emit machine-readable JSON instead of text |
| `--insecure` | off | Skip TLS verification (not recommended) |
| `-h`, `--help` | — | Show help and exit |

---

## 6. Common recipes

**Size the 2023 orphans only:**
```bash
./orphaned_searchable_snapshots.py --cluster prod --pattern '2023.*' --report-size
```

**See the biggest offenders:**
```bash
./orphaned_searchable_snapshots.py --cluster prod --report-size --per-snapshot
```

**Save a JSON report (clean stdout):**
```bash
./orphaned_searchable_snapshots.py --cluster prod --report-size --json > orphan_size.json
```

**Delete the 2023 orphans after reviewing them:**
```bash
# review first (dry-run)
./orphaned_searchable_snapshots.py --cluster prod --pattern '2023.*'
# then delete
./orphaned_searchable_snapshots.py --cluster prod --pattern '2023.*' --apply
```

**Secret in a non-default region / custom name:**
```bash
./orphaned_searchable_snapshots.py --cluster prod --region us-east-1
./orphaned_searchable_snapshots.py --secret-name elastic/kibana/dataview_cleanup_prod
```

---

## 7. Interpreting the numbers

- Report **`incremental (free)`** as the expected savings — it accounts for deduplication
  between snapshots. For the force-merged searchable snapshots here there is usually little
  sharing, so `total` and `incremental` tend to be close.
- For the **exact repository bill**, cross-check the backing object-storage bucket metrics
  (AWS S3 `BucketSizeBytes`, or the GCS/Azure equivalent) for the deployment's repository
  path — ground truth that sidesteps dedup-counting.

---

## 8. Performance & safety notes

- **Dry-run by default.** Nothing is deleted unless you pass `--apply`. Always run once
  without `--apply` (optionally with `--report-size`) and review the orphan list first.
- The `_status` API is **heavy and blocking**. Requests are already batched by count and
  URL length; if you see contention with regular SLM snapshots, lower `--batch` (e.g.
  `--batch 20`) and/or scope with `--pattern`.
- Deletion is irreversible. The tool only deletes snapshots that are **not referenced by
  any mounted index** — but a dry-run review is still recommended before `--apply`.

---

## 9. Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| `error: unrecognized arguments: --cluster` | You're running an old copy — pull the latest. |
| `too_long_http_line_exception` / HTTP 400 on `_status` or DELETE | Fixed by URL-length batching; if it recurs on an unusual repo, lower `--batch`. |
| `ERROR: no Elasticsearch endpoint resolved...` | Pass `--cluster`, `--es-url`, or export `ES_URL`. |
| `ERROR: no API key resolved...` | Pass `--cluster`, `--api-key`, or export `ES_API_KEY`. |
| `ERROR: AWS secret '...' is missing key(s): es_api_key` | Add both `es_url` and `es_api_key` to the secret JSON. |
| `ERROR: reading AWS Secrets Manager needs either boto3 or the aws CLI` | `pip install boto3` or install the `aws` CLI. |
| `ERROR: failed to read AWS secret '...'` | AWS creds/permissions or region — check `secretsmanager:GetSecretValue` and `--region`. |
| `HTTP 401` / `403` | API key invalid or lacks privileges — `monitor` + `view_index_metadata` (and `manage` for `--apply`). |
| `HTTP 404` on `_snapshot/...` | Wrong repository name — set `--repo`. |
| Runs slowly / affects the cluster | Lower `--batch`, scope with `--pattern`, or run off-peak. |

---

## 10. Related files

- `analyze_ilm.py` — audits ILM policies for the missing/`false` `delete_searchable_snapshot`
  setting that creates these orphans in the first place.
- `corrected_ilm_policies/` — ready-to-apply `PUT _ilm/policy` bodies that add a delete
  phase to the leaking policies.
- `searchable_snapshot_ilm_findings.md` — the full audit write-up and background.
