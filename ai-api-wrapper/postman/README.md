# Postman collection

`ARIA-extraction-job.postman_collection.json` — five requests against the ai-api-wrapper
service.

## Setup

**1. Start the service**

```bash
cd <repo root>
.venv/bin/python -m uvicorn ai-api-wrapper.api:app --port 8001
```

Note the module path uses **dots**, not slashes.

**2. Import the collection** into Postman (File → Import).

**3. Set the `apiKey` collection variable.** Read it with:

```bash
.venv/bin/python -c "from dotenv import dotenv_values; print(dotenv_values('ai-api-wrapper/.env')['ARIA_API_KEY'])"
```

This is `ARIA_API_KEY` — the key that guards *this service's* endpoint. **Not** the storage
account key. The two must never be the same value: `ARIA_API_KEY` gets shared with the web app
team, and the storage key grants full read/write over the whole account.

**4. Point `baseUrl` at the right host.** Defaults to `http://localhost:8001`; on the VM use
`http://<vm-public-ip>:8001`.

## The requests

| # | Request | Asserts | Auth |
|---|---|---|---|
| 1 | Health | 200, `status: ok` | none |
| 2 | Ready | 200, logs the credential mechanism | none |
| 3 | **Submit job** | **202**, `ACCEPTED`, echoes `job_id` | `X-API-Key` |
| 4 | Poll job | 200, logs status and detail | none |
| 5 | Auth check | **401** — proves the endpoint is not open | none (deliberately) |

Run them in order. All five status-code assertions are verified against the live service.

### `jobId` is generated per run

A pre-request script sets a fresh `jobId` on every Submit. That matters because uploads use
`overwrite=true` — reusing an id would replace a previous run's `output.json` and `status.json`.
Each run gets its own output folder.

### 202 is not a result

Extraction takes minutes, so Submit returns immediately with `202 ACCEPTED` and the work
continues in the background. Learn the outcome by either:

- re-sending request 4 until it leaves `QUEUED`/`RUNNING`, or
- reading `status.json` at the `status_url` returned by Submit — **the durable answer**

Request 4 reads in-memory state and is lost when the service restarts. `status.json` in Blob
Storage survives.

## Expected result today: FAILED

Submit returns 202, then the job fails:

```json
{
  "status": "failed",
  "error_type": "BlobAccessError",
  "detail": "download failed for .../resource-master/Resource%20Master.xlsx: BlobNotFound"
}
```

**This is correct behaviour, not a bug in the collection.** The extraction needs *both* an AMG
`.docx` and a Resource Master `.xlsx`.

| Input | Status |
|---|---|
| AMG `.docx` | ✅ exists — `20260917_1789682018392/ARIA Guide NBL AMG…MAR 2026….docx`, 1,777,711 bytes, URL verified |
| Resource Master `.xlsx` | ❌ **missing** — no `.xlsx` exists anywhere in `ariallminput` (74 blobs checked) |

### To make it go green

Ask infra to upload a Resource Master to:

```
ariallminput/resource-master/Resource Master.xlsx
```

Or point request 3's second `input_urls` entry at wherever they put it. Nothing else needs to
change — the AMG URL, auth, and job flow are all confirmed working.

Once it succeeds you will get `status: SUCCEEDED` and a workbook at the `artifact_url` in
`output.json`. Note that until `run_pipeline()` is implemented, that workbook is a **placeholder**
— the manifest says so explicitly (`"implementation": "DUMMY"`, `needs_review: true`).

## For the web app team

Request 3 is the contract. Same body, same header, different host:

```
POST http://<vm-ip>:8001/jobs
X-API-Key: <ARIA_API_KEY>
Content-Type: application/json

{ "job_id": "...", "input_urls": [...], "output_url": "...", "email_url": "..." }
```

Treat `202` as *accepted, not finished*, then poll the returned `status_url` until it reads
`succeeded` or `failed`.
