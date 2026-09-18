# `scripts/` — the ARIA extraction job

A worker that takes a clinical guideline document out of Azure Blob Storage, extracts its
content into an ARIA data template, and writes the result back to Blob Storage.

It is a **batch worker**, not a web service. Something hands it one job, it does that job, it
exits.

---

## Status at a glance

| Capability | State |
|---|---|
| Download inputs from Blob Storage | ✅ built, tested against the live account |
| Validate the downloads are real documents | ✅ built |
| Run the extraction | 🔲 **placeholder** — awaiting the AI team |
| Upload the result workbook | ✅ built, tested |
| Write a run manifest (`output.json`) | ✅ built, tested |
| Write job status (`status.json`) | ✅ built, tested |
| **Send an email** | ❌ **not built** — see [Gap 1](#gap-1--nothing-sends-an-email) |
| **Receive jobs from the web app** | ❌ **not built** — see [Gap 2](#gap-2--nothing-delivers-jobs-to-the-vm) |

The storage plumbing is finished and proven. Two integration points and the extraction itself
are outstanding, each with a named owner in [Who does what](#who-does-what).

---

## The three files

| File | Role | Who edits it |
|---|---|---|
| `run_job.py` | **the boss.** Knows the order of steps, handles errors, sets exit codes | nobody, normally |
| `blob_io.py` | **the delivery driver.** Credentials, download, upload. The only module that touches the network | nobody, normally |
| `aria_pipeline.py` | **the AI slot.** Input rules + `run_pipeline()` | **the AI team** |

The dependency shape matters:

```
                run_job.py          ← knows the SEQUENCE
               /          \
              ↓            ↓
       blob_io.py     aria_pipeline.py
   (knows Azure,       (knows ARIA,
    nothing of ARIA)    nothing of Azure)
```

`blob_io.py` and `aria_pipeline.py` **never import each other.** The extraction can be rewritten
without touching storage code, and storage can be swapped without touching the extraction.

### Self-contained on purpose

This package imports nothing from `dennis/` or `config/`. Both are gitignored, so a committed
script that depended on them would crash on every other machine. That constraint is also why
`run_pipeline()` is a placeholder rather than a call into the prototype populators.

---

## What a job looks like

```json
{
  "job_id":     "20260917_1789680857222",
  "input_urls": ["https://<account>.blob.core.windows.net/ariallminput/...AMG....docx",
                 "https://<account>.blob.core.windows.net/ariallminput/...Resource Master....xlsx"],
  "output_url": "https://<account>.blob.core.windows.net/ariallmoutput/<job_id>/output.json",
  "email_url":  "https://<account>.blob.core.windows.net/ariallmoutput/<job_id>/status.json"
}
```

**No URL is hardcoded anywhere in the code.** They arrive in this payload at runtime, which is
why one deployment can serve all ~40 guidelines. The payload is read from a file (`--job`) or
from stdin.

`output_url` and `email_url` are what make this asynchronous: the caller is told *where to look
for results* rather than waiting for a reply.

---

## The flow, step by step

```
job payload ──► run_job.py ──► download ──► run_pipeline() ──► upload ──► status
```

| # | What happens | Where | Calls into |
|---|---|---|---|
| 1 | read + validate the payload | [run_job.py:106](run_job.py#L106) | — (fails before any network call) |
| 2 | pick the credential, **once** | [blob_io.py:91](blob_io.py#L91) | — |
| 3 | make a per-job temp folder | [run_job.py:143](run_job.py#L143) | — |
| 4 | write `status.json` = **running** | [run_job.py:147](run_job.py#L147) | `blob_io.upload_json` |
| 5 | download each input | [run_job.py:152](run_job.py#L152) | `blob_io.download` |
| 6 | check each is a real ZIP/Office file | [blob_io.py:146](blob_io.py#L146) | — |
| 7 | identify which file is which | [run_job.py:162](run_job.py#L162) | `aria_pipeline.classify_inputs` |
| 8 | **run the extraction** | [run_job.py:165](run_job.py#L165) | `aria_pipeline.run_pipeline` |
| 9 | confirm the artifact really exists | [run_job.py:166](run_job.py#L166) | — |
| 10 | upload the workbook | [run_job.py:174](run_job.py#L174) | `blob_io.upload_file` |
| 11 | upload `output.json` manifest | [run_job.py:184](run_job.py#L184) | `blob_io.upload_json` |
| 12 | write `status.json` = **succeeded** | [run_job.py:188](run_job.py#L188) | `blob_io.upload_json` |
| 13 | delete the temp folder — **always** | [run_job.py:218](run_job.py#L218) | — |

### Only two calls cross into the AI code

```python
inputs = classify_inputs(downloaded)   # list[Path] → JobInputs
result = run_pipeline(inputs, work)    # JobInputs  → PipelineResult
```

Both trade in **local file paths, never URLs.** By step 7 the blobs are already on local disk,
so `aria_pipeline.py` has no idea Azure exists. That is the entire interface.

### Why the step order is what it is

- **Payload validated before any network call** (step 1 before 2) — a malformed job costs
  nothing and can never half-run.
- **`status.json` written twice** (steps 4 and 12). Without the first write, a job killed
  mid-run by OOM or node eviction leaves no trace and looks identical to one never submitted.
- **Status written last, after both uploads** (step 12). It is the signal a watcher acts on, so
  it must not claim success before the artifact is durably stored.
- **The artifact is verified even though the pipeline said it succeeded** (step 9). Otherwise a
  pipeline bug surfaces as an upload error and blames storage.
- **Temp folder removed on every path including failure** (step 13). Guideline documents are not
  ours to redistribute and the VM is shared.

---

## What gets written

```
ariallmoutput/<job_id>/ARIA Data Template - GENERATED.xlsx    the deliverable
ariallmoutput/<job_id>/output.json                            manifest — what ran, what it made
ariallmoutput/<job_id>/status.json                            running → succeeded | failed
```

`output_url` names a `.json` but the deliverable is a workbook, so one blob cannot be both. The
manifest stays at that exact URL — anything already polling it keeps working — and carries
`artifact_url` pointing at the workbook beside it.

### Exit codes

| Code | Meaning | `status.json` |
|---|---|---|
| `0` | succeeded | `succeeded` |
| `1` | pipeline or storage failure | `failed`, with the reason |
| `2` | bad payload or no credential | **not written** — nothing was attempted |

Code `2` writes no status deliberately: without a valid payload or credential there is nowhere
to write one.

---

## Running it

### On your laptop

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r scripts/requirements.txt

# put AZURE_STORAGE_ACCOUNT and AZURE_STORAGE_KEY in a gitignored .env
cp scripts/.env.example .env      # then fill in the key

# READ-ONLY. Confirms credential + input readability. Writes nothing.
USE_MANAGED_IDENTITY=0 .venv/bin/python scripts/run_job.py \
    --job scripts/jobs.json --env-file .env --check
```

Expected:

```
check: auth account key
check: readable  <input 1> (N bytes)
check: readable  <input 2> (N bytes)
check: writable target accepted for output_url
check: writable target accepted for email_url
check: OK
```

**`USE_MANAGED_IDENTITY=0` is required on a laptop.** Without it the code looks for an Azure
machine identity, finds none, and works through nine fallbacks with timeouts — an ~80 second
hang and a wall of red text. See [Troubleshooting](#troubleshooting).

Drop `--check` for a real run. It will write to the output container.

### On the VM

```bash
git clone <repo> && cd KIDS26-Team3
python3.12 -m venv .venv
.venv/bin/pip install -r scripts/requirements.txt

export AZURE_STORAGE_ACCOUNT=<account>       # not a secret; it is in every job URL

.venv/bin/python scripts/run_job.py --job jobs.json --check
```

**No `--env-file`, no `USE_MANAGED_IDENTITY`, no key.** The VM authenticates as itself. Look for
`check: auth DefaultAzureCredential` — that confirms machine identity is in use.

`dennis/` will not exist on the VM. That is expected.

### Is the VM's identity actually assigned?

SSH in and ask the machine directly. Needs no root:

```bash
curl -s -H "Metadata:true" --max-time 5 \
 "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https://storage.azure.com/" \
 | head -c 120; echo
```

| Result | Meaning |
|---|---|
| `{"access_token":"eyJ0...` | ✅ assigned — deploy |
| `{"error":...` or a timeout | ❌ not assigned — go back to infra |

A token plus a later permissions error means the identity exists but the **role assignments**
are missing — a different infra ask.

---

## Credentials

Resolution order in [`blob_io.py:91`](blob_io.py#L91):

1. **`DefaultAzureCredential`** — production. Managed identity. **The default.**
2. `AZURE_STORAGE_SAS_TOKEN` — scoped and expiring. Preferred for local work.
3. `AZURE_STORAGE_KEY` — full account access, no expiry. Local last resort.

Managed identity is **first**, not last. A deployed job should never carry a long-lived account
key: it cannot be rotated without a redeploy, it grants far more than one job needs, and it
lands in `container inspect` output and crash dumps. `USE_MANAGED_IDENTITY=0` opts out locally —
mild friction that marks doing the less-secure thing on purpose.

### Three safeguards

- **No credential is ever logged.** `credential_summary()` returns the mechanism, not the
  secret, and the Azure SDK loggers are pinned to `WARNING` in `run_job.py` — at `DEBUG` they log
  the `Authorization` header, so a raised `LOG_LEVEL` must not be able to leak it.
- **Account mismatch is fatal** ([`blob_io.py:108`](blob_io.py#L108)). A URL naming a different
  account than the credential is scoped to stops the job. Letting it proceed produces
  `AuthenticationFailed`, which sends whoever is on call hunting through permissions instead of
  reading the job payload. Job URLs are untrusted input from another system; this is input
  validation.
- **Downloads are ZIP-header checked** ([`blob_io.py:145`](blob_io.py#L146)). A `.docx`/`.xlsx`
  is a ZIP container. A proxy or SAS error page returned with HTTP 200 and saved under that name
  fails here, loudly, instead of deep inside a document parser.

---

## For the AI engineers

You own **one function**. Everything around it is built and tested.

**File:** `aria_pipeline.py` · **Function:** [`run_pipeline()`](aria_pipeline.py#L170) · search
for `EXTENSION POINT`

### The contract

```
in : JobInputs — an AMG .docx and a Resource Master .xlsx, already downloaded to local
     disk and verified to be real Office files, plus a writable work_dir
out: PipelineResult whose .artifact is a file that EXISTS
err: raise. run_job.py catches it, writes the status blob, exits non-zero.
```

```python
def run_pipeline(inputs: JobInputs, work_dir: Path) -> PipelineResult:
    # inputs.amg              -> Path to the guideline .docx
    # inputs.resource_master  -> Path to the Resource Master .xlsx
    # work_dir                -> scratch space, yours to write in

    return PipelineResult(
        artifact=<path to the workbook you created>,
        details={...},              # lands verbatim in output.json
        needs_review=<bool>,
    )
```

**Raise on failure — never return a partial result.** A caller cannot tell a partial result from
a successful one.

You never touch Azure, never see a URL, never handle a credential.

### The four extension points

| # | Where | What to do |
|---|---|---|
| 1 | [`run_pipeline()` body](aria_pipeline.py#L226) | the extraction. Delete everything between the `DUMMY IMPLEMENTATION` markers |
| 2 | top of `run_pipeline()` | fetch model keys from **Key Vault** at run start |
| 3 | `PipelineResult.confidence` / `needs_review` | the quality gate |
| 4 | `RESOURCE_MASTER_PATTERNS` / `AMG_PATTERNS` | only if new input types appear |

Also: **tell the maintainer your dependencies** (`python-docx`, an LLM SDK, …) so they go into
`requirements.txt`.

### Model keys belong in Key Vault

Not in environment variables, not in a committed file.

```python
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
kv = SecretClient(vault_url=os.environ["KEY_VAULT_URL"],
                  credential=DefaultAzureCredential())
api_key = kv.get_secret(os.environ["MODEL_SECRET_NAME"]).value
```

The endpoints doc requires it; Key Vault access needs campus network or Cloudflare, so a job
reading keys at startup **fails fast** rather than midway through an extraction; and a key in an
env var appears in `container inspect` and crash dumps. Never log it, never put it in
`PipelineResult.details`, and never send guideline text or clinical data to a model the team has
not approved (`docs/ai-guidance.md`).

### Two lessons from the prototype

**Locate content by section heading, never by table number.** The same content is numbered
differently in every document — across two drafts of the *same* guideline the pathology table
moved from `Table 8` to `Table 9` — and some guidelines carry prose where others carry a table.
Walk the document by heading and report whatever the section contains.

**Have each populator own exactly one target sheet and update the workbook in place.** Rebuilding
it per populator means whichever ran last is the only one with output.

### What the dummy does on purpose

- writes a **real `.xlsx`**, not a text stub — that exercises the binary upload path and the
  content-type header, the two things most likely to be wrong on first deploy;
- sets **`needs_review=True`**, so nothing downstream can mistake a placeholder for a real
  deliverable.

---

## For the infra engineers

### Needed to deploy

**1. A VM with a managed identity**, and two role assignments:

| Container | Role |
|---|---|
| `ariallminput` | Storage Blob Data **Reader** |
| `ariallmoutput` | Storage Blob Data **Contributor** |

This means **no key is installed on the VM** — nothing to leak, nothing to rotate.

**2. One environment variable:** `AZURE_STORAGE_ACCOUNT=<account>`. Not a secret; it is in every
job URL. It powers the account-mismatch check above.

**3. Python 3.12** and outbound network access to `<account>.blob.core.windows.net`.

**4. Later:** a **Key Vault** reachable from the VM, for the AI model keys.

**5. Real test inputs.** Please upload one genuine AMG `.docx` and the Resource Master `.xlsx` to
`ariallminput`. The current test blobs are 26- and 16-byte placeholders, so the real-document
path has never been exercised.

### Please run `--check` first

It confirms the credential works, the inputs are readable, and the output targets are valid —
without writing anything a downstream consumer might act on.

---

## The two gaps

### Gap 1 — nothing sends an email

The job writes `status.json` to the address given in `email_url`:

```json
{ "job_id": "...", "status": "succeeded", "updated_utc": "...", "artifact_url": "..." }
```

The field is *named* `email_url`, but it points at a blob, not a mailbox. **No code anywhere
sends mail.** So:

> **Question for infra: is something already watching that blob and sending the email —
> a Logic App, a Function, an Event Grid subscription?**

- **If yes** — nothing to build. Done.
- **If no** — someone must add it, and we should decide where it lives.

**Recommendation: keep it outside this job.** Let the job record what happened and let a separate
watcher turn that into an email. Then a mail outage cannot fail an extraction that actually
succeeded, and email retry logic stays away from extraction retry logic.

If it must live here, it is a small addition to `write_status` using
`azure-communication-email` — roughly 20 lines. Not worth writing until Gap 1 is answered.

### Gap 2 — nothing delivers jobs to the VM

```
  WEB APP                        AZURE                      VM
  ───────                        ─────                      ──
  uploads files ────────►  ariallminput/        ✅
  builds job JSON ──────►     ???  ◄──────────  ???     nothing is listening
                                  THE GAP
                           ariallmoutput/  ◄───  results  ✅
  polls for result ◄─────   status.json        ✅
```

Today a human hands the script a `jobs.json`. There is no automated path from the web app to the
worker.

The payload format is a clue that the web app side may already exist:

```
"job_id": "20260917_1789680857222"      ← date + millisecond timestamp, machine-generated
```

**Question for the web app team: do you already upload the inputs and build this payload, and
how did you expect to hand the job off?**

#### Options

| Option | How | Verdict |
|---|---|---|
| **Storage Queue** | web app posts a message; VM polls it | ✅ **recommended** |
| Blob trigger | web app writes job JSON to a `jobs/` container; VM polls | workable |
| Event Grid → webhook | Azure POSTs to the VM | VM becomes a web server |
| Web app POSTs to the VM | direct call to the VM's IP | ❌ avoid |

**Why Storage Queue:** the VM never accepts inbound connections — it only makes outgoing calls.
The VM has a **public IP**, so an HTTP endpoint there would need auth, TLS, and patching. Polling
avoids all of it, and brings retries (a crashed job's message reappears), buffering, and
decoupling. Managed identity already covers queues, so no new secret.

**To close it:** infra creates one queue (e.g. `aria-jobs`) and grants the VM's identity *Storage
Queue Data Message Processor*. The web app adds one API call after upload. The maintainer adds a
~30-line poller around the existing `handle_job`, plus `azure-storage-queue` in requirements; a
user-level `crontab` entry runs it, so no root is needed.

---

## Who does what

```
Infra: VM + managed identity + role assignments
         │
         ▼
Me: deploy skeleton, run --check              ← proves the infrastructure
         │
         ├── Infra: upload real .docx + .xlsx
         ├── Infra: answer the email question (Gap 1)
         ├── Infra + web app: create the queue (Gap 2)
         ├── AI team: implement run_pipeline()
         ▼
Me: real end-to-end run
         │
         ▼
Infra: confirm the email fires
```

**Deploy the skeleton before the AI code is ready.** The dummy pipeline proves the VM, the
identity, the network and the storage all work while the extraction is still being built. Then
swapping in the real pipeline is a one-file change — and if it breaks, you know it is the
extraction, not the infrastructure. Two small milestones instead of one risky one.

| Owner | Outstanding |
|---|---|
| **Infra** | managed identity + 2 roles · Python 3.12 · real test inputs · **Gap 1 answer** · queue for Gap 2 · Key Vault |
| **AI team** | `run_pipeline()` · Key Vault wiring · declare dependencies |
| **Maintainer** | deploy · `--check` on the VM · queue poller · add AI dependencies |

---

## Architecture decisions, and why

**It is a batch worker, not a web service.** The `output_url`/`email_url` pair proves the caller
expects fire-and-forget: it is told where to look rather than waiting. Extraction takes minutes;
HTTP times out in seconds.

**The extraction is an in-process function call, not a separate service.** The inputs are local
files — a 2MB Word document already on disk — so a service boundary would mean uploading it again
or sharing storage between two deployments. One deployment, one log stream, and nothing here
needs independent scaling. The seam is a single function, so it can become a remote call later
without touching `run_job.py` or `blob_io.py`. Split it only if the AI work needs different
hardware, has conflicting dependencies, or must release independently.

**Input classification refuses to guess.** Two `.docx` files both matching the AMG pattern is an
error, not a coin flip — drafts differ in content and table numbering, and picking the wrong one
produces plausible-looking wrong data.

**Unexpected exceptions log the traceback but write only the message** to the status blob. A
traceback can carry file paths and payload fragments, and that blob may be read outside the team.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: No module named 'azure'` | wrong Python | use `.venv/bin/python` |
| **Hangs silently, no output** | no `--job`, so it is reading stdin — i.e. your keyboard | pass `--job <file>`; `Ctrl-C` to escape |
| **~80s hang, then a wall of `DefaultAzureCredential failed`** | laptop has no machine identity | add `USE_MANAGED_IDENTITY=0` |
| `job payload is not valid JSON: line 2 column 1` | **non-breaking spaces** from copy-pasting JSON | replace ` ` with real spaces |
| `no .docx among inputs [...]` | input container holds placeholders, not real documents | upload a real AMG + Resource Master |
| `blob URL account 'x' does not match ...` | payload and environment disagree | check the job payload, not permissions |
| `... is not a valid .docx/.xlsx (no ZIP header)` | an error page was saved under that name | check the URL or SAS validity |

**The one line to always read:**

```
check: auth account key              ← key mode. Correct on a laptop.
check: auth DefaultAzureCredential   ← machine identity. Correct on the VM.
```

If it says `DefaultAzureCredential` on your laptop, you forgot `USE_MANAGED_IDENTITY=0`.

### The job file is not auto-discovered

`--job` has **no default**. Creating `scripts/jobs.json` does not make the script find it — there
is no glob, no implicit lookup. You must point at it. Without `--job` it reads stdin.

---

## Verified

| Check | Result |
|---|---|
| Missing payload fields / malformed JSON | exit **2**, no network touched |
| Input classification, 6 scenarios | all correct — including the 2-`.docx` refusal |
| Dummy pipeline | valid 2-sheet workbook, `needs_review=True` |
| `--check` against the **live** account | exit 0 — both input blobs readable |
| Full round trip (via the prototype runner) | 41KB `.xlsx` + `output.json` + `status.json` confirmed in Azure, correct content types |
| Secret scan, compile check | clean |

### Not yet verified

- **Managed identity on the VM** — only the account-key path has been exercised, from a laptop.
  `--check` on the VM is what confirms it.
- **A real guideline flowing from Blob Storage.** The successful round trip used a document from
  a local folder, because the input container held placeholders. In that run the prototype
  reported `"used_local_amg_fallback": true`. The production code here has **no such fallback** —
  it fails instead, by design.
- **The email**, since nothing sends one yet.
- **Any automated trigger.** Every run so far has been started by hand.
