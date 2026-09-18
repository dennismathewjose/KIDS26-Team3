#!/usr/bin/env python3
"""HTTP front end for the ARIA extraction job.

    uvicorn scripts.api:app --host 0.0.0.0 --port 8000
    uvicorn scripts.api:app --reload --port 8000        # local development

    POST /jobs      submit a job. Returns 202 ACCEPTED immediately.
    GET  /jobs/{id} what happened to a job this process accepted
    GET  /health    liveness. No network, no auth. For a load balancer.
    GET  /ready     readiness. Confirms a credential resolves.


TWO ENTRY POINTS, ONE CORE
--------------------------
    run_job.py   CLI / queue worker  ──┐
    api.py       HTTP                  ──┴──►  handle_job()

This module adds a transport. It contains no extraction logic, no blob logic and no job logic
of its own -- it validates a request, hands it to `run_job.handle_job`, and reports back.
`run_job.py` stays usable on its own, which matters because the CLI is how you debug and how a
cron or queue trigger would drive this.


WHY 202 AND NOT A RESULT
------------------------
An extraction takes minutes. A synchronous handler would hold the connection open until a
proxy, load balancer or client timed out -- and the job would keep running anyway, so the
caller would have no idea whether it succeeded.

So the request is validated, queued, and acknowledged with 202 plus the `job_id`. The work
happens on a background thread. The caller then learns the outcome exactly the way the batch
path already publishes it:

    * poll `status.json` at the job's `email_url`      <- authoritative, survives restarts
    * or poll GET /jobs/{job_id}                       <- convenience, in-memory only

`status.json` is the real answer. `/jobs/{id}` is a courtesy that disappears on restart,
because this process deliberately keeps no database.


CONCURRENCY IS BOUNDED
----------------------
Each job downloads documents, writes temp files and burns CPU. Unbounded acceptance would let
a burst of submissions exhaust the VM's disk or memory, which fails every in-flight job rather
than just the excess ones. MAX_CONCURRENT_JOBS caps it and a full queue returns 429 with
Retry-After, so the caller can back off instead of guessing.


AUTHENTICATION IS REQUIRED BY DEFAULT
-------------------------------------
The VM has a public IP. An open POST endpoint there is an open invitation to make the service
fetch and write arbitrary blobs in your storage account.

Set ARIA_API_KEY and send it as `X-API-Key`. Startup FAILS if it is unset, unless
ARIA_ALLOW_ANONYMOUS=1 is set explicitly -- opting out has to be a decision someone typed, not
a default someone forgot.

This is a shared secret, which is the floor, not the goal. Prefer putting the service behind
something that does real authentication (App Gateway, API Management, an internal-only NSG)
and treat this as defence in depth.
"""

from __future__ import annotations

import logging
import os
import secrets
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent))

from blob_io import BlobAccessError, BlobConfig, BlobStore    # noqa: E402
from run_job import REQUIRED_FIELDS, handle_job, setup_logging, utc_now   # noqa: E402

log = logging.getLogger("aria.api")

MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "2"))

app = FastAPI(
    title="ARIA extraction job",
    version="0.1.0",
    description="Submit an extraction job. Results land in Blob Storage, not in the response.",
)

# Module state, created once at startup rather than per request: resolving a credential costs
# a token fetch, and doing that on every call would add latency and hammer the IMDS endpoint.
_store: BlobStore | None = None
_pool: ThreadPoolExecutor | None = None
_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()


# ────────────────────────────────────────────────────────── request model

class JobRequest(BaseModel):
    """The same payload the CLI takes. Pydantic rejects a malformed body with 422.

    `extra` fields are allowed: the submitting system may add its own metadata, and a
    strict model would reject jobs over a field this service does not care about.
    """

    job_id: str = Field(min_length=1, max_length=200)
    input_urls: list[str] = Field(min_length=1)
    output_url: str = Field(min_length=1)
    email_url: str = Field(min_length=1)

    model_config = {"extra": "allow"}


class JobAccepted(BaseModel):
    job_id: str
    status: str = "ACCEPTED"
    submitted_utc: str
    status_url: str
    poll_url: str


# ─────────────────────────────────────────────────────────────────── auth

def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Shared-secret check on the mutating endpoint.

    `compare_digest` rather than `==`: string comparison short-circuits on the first
    differing byte, which leaks the key's prefix to anyone timing the responses.
    """
    expected = os.environ.get("ARIA_API_KEY", "")
    if not expected:                       # startup already allowed this explicitly
        return
    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        # 401 without detail: saying "wrong key" vs "no key" tells a prober which half to fix.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="missing or invalid X-API-Key")


# ────────────────────────────────────────────────────────────── lifecycle

@app.on_event("startup")
def on_startup() -> None:
    global _store, _pool
    setup_logging()

    if not os.environ.get("ARIA_API_KEY"):
        if os.environ.get("ARIA_ALLOW_ANONYMOUS") != "1":
            raise RuntimeError(
                "ARIA_API_KEY is not set. This service accepts jobs that read and write your "
                "storage account, and the VM has a public IP.\n"
                "  Set ARIA_API_KEY=<secret>, or set ARIA_ALLOW_ANONYMOUS=1 to accept the risk "
                "deliberately (local development only).")
        log.warning("ARIA_ALLOW_ANONYMOUS=1 -- /jobs is UNAUTHENTICATED")

    # Fail at startup, not on the first request: a credential problem should stop the deploy
    # and show up in the container logs, rather than surfacing as a 500 to the web app.
    _store = BlobStore(BlobConfig.from_env())
    _pool = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_JOBS,
                               thread_name_prefix="aria-job")
    log.info("api ready: auth=%s max_concurrent=%d",
             _store.credential_summary(), MAX_CONCURRENT_JOBS)


@app.on_event("shutdown")
def on_shutdown() -> None:
    if _pool is not None:
        # Let running extractions finish rather than orphaning a half-written output blob.
        log.info("shutdown: waiting for in-flight jobs")
        _pool.shutdown(wait=True)


# ────────────────────────────────────────────────────────────── endpoints

@app.get("/health")
def health() -> dict:
    """Liveness only: is the process up. No auth, no network, no credential.

    Deliberately cheap. A load balancer may hit this every few seconds, so anything that
    touched Azure here would turn monitoring into sustained load and into a bill.
    """
    return {"status": "ok", "utc": utc_now()}


@app.get("/ready")
def ready() -> dict:
    """Readiness: is the process able to do work.

    Reports the credential MECHANISM, never the secret.
    """
    if _store is None or _pool is None:
        raise HTTPException(status_code=503, detail="not initialised")
    with _jobs_lock:
        running = sum(1 for j in _jobs.values() if j["status"] == "RUNNING")
    return {
        "status": "ready",
        "auth": _store.credential_summary(),
        "running": running,
        "max_concurrent": MAX_CONCURRENT_JOBS,
    }


@app.post("/jobs", status_code=status.HTTP_202_ACCEPTED, response_model=JobAccepted,
          dependencies=[Depends(require_api_key)])
def submit_job(request: JobRequest, response: Response) -> JobAccepted:
    """Accept a job and run it in the background. Returns 202, never a result."""
    if _store is None or _pool is None:
        raise HTTPException(status_code=503, detail="not initialised")

    job = request.model_dump()
    missing = [f for f in REQUIRED_FIELDS if not job.get(f)]
    if missing:                                     # belt and braces alongside Pydantic
        raise HTTPException(status_code=422, detail=f"missing field(s): {missing}")

    with _jobs_lock:
        existing = _jobs.get(job["job_id"])
        if existing and existing["status"] in ("QUEUED", "RUNNING"):
            # Idempotency: a web app retrying a timed-out POST must not start the same
            # extraction twice, which would have both runs writing the same output blob.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"job {job['job_id']} is already {existing['status']}")

        running = sum(1 for j in _jobs.values() if j["status"] == "RUNNING")
        if running >= MAX_CONCURRENT_JOBS:
            response.headers["Retry-After"] = "60"
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"{running} job(s) already running (max {MAX_CONCURRENT_JOBS})")

        _jobs[job["job_id"]] = {"status": "QUEUED", "submitted_utc": utc_now(),
                                "exit_code": None, "detail": None}

    _pool.submit(_run, job)
    log.info("accepted job %s", job["job_id"])

    return JobAccepted(
        job_id=job["job_id"],
        submitted_utc=_jobs[job["job_id"]]["submitted_utc"],
        status_url=job["email_url"],      # the authoritative answer lives here
        poll_url=f"/jobs/{job['job_id']}",
    )


@app.get("/jobs/{job_id}")
def job_state(job_id: str) -> dict:
    """In-memory state for a job THIS process accepted.

    404 does not mean the job never existed -- only that this process has no record of it.
    The durable answer is `status.json` in Blob Storage; see the module docstring.
    """
    with _jobs_lock:
        record = _jobs.get(job_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"no record of {job_id} in this process; check status.json in Blob Storage")
    return {"job_id": job_id, **record}


# ──────────────────────────────────────────────────────────── the worker

def _run(job: dict) -> None:
    """Run one job on a pool thread. Never raises -- a thread that dies is invisible."""
    job_id = job["job_id"]
    started = datetime.now(timezone.utc)
    with _jobs_lock:
        _jobs[job_id].update(status="RUNNING", started_utc=utc_now())
    try:
        exit_code = handle_job(job, _store)          # the exact same call the CLI makes
        with _jobs_lock:
            _jobs[job_id].update(
                status="SUCCEEDED" if exit_code == 0 else "FAILED",
                exit_code=exit_code,
                finished_utc=utc_now(),
                duration_seconds=round(
                    (datetime.now(timezone.utc) - started).total_seconds(), 1))
    except BlobAccessError as exc:
        log.error("job %s storage failure: %s", job_id, exc)
        _record_crash(job_id, exc, started)
    except Exception as exc:                          # noqa: BLE001
        # handle_job already catches its own failures, so reaching here means something
        # unexpected. Swallowing it would leave the job stuck on RUNNING forever.
        log.exception("job %s crashed outside handle_job", job_id)
        _record_crash(job_id, exc, started)


def _record_crash(job_id: str, exc: BaseException, started: datetime) -> None:
    with _jobs_lock:
        _jobs[job_id].update(
            status="FAILED", exit_code=1,
            detail=f"{type(exc).__name__}: {exc}",
            finished_utc=utc_now(),
            duration_seconds=round(
                (datetime.now(timezone.utc) - started).total_seconds(), 1))
