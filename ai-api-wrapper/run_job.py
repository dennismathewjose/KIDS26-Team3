#!/usr/bin/env python3
"""ARIA extraction job runner.

    input blobs  ->  download  ->  run_pipeline  ->  output blob  +  status blob

    # from a job file
    python scripts/run_job.py --job job.json

    # from the payload on stdin (queue trigger, container arg)
    echo "$JOB_JSON" | python scripts/run_job.py

    # verify wiring without running the pipeline or writing the output blob
    python scripts/run_job.py --job job.json --check

This module is the orchestration only. The extraction lives in `aria_pipeline.run_pipeline`,
which is currently a dummy -- see that file's header. Nothing here should need editing when
the AI pipeline is added.


JOB PAYLOAD
-----------
    {
      "job_id":      "20260917_1789680857222",
      "input_urls":  ["https://<account>.blob.core.windows.net/<in>/...docx",
                      "https://<account>.blob.core.windows.net/<in>/...xlsx"],
      "output_url":  "https://<account>.blob.core.windows.net/<out>/<job>/output.json",
      "email_url":   "https://<account>.blob.core.windows.net/<out>/<job>/status.json"
    }


WHAT GETS WRITTEN
-----------------
    <output dir>/<artifact>.xlsx    the populated template
    <output dir>/output.json        manifest, AT output_url unchanged
    <output dir>/status.json        running -> succeeded | failed, AT email_url

`output_url` names a .json but the deliverable is a workbook, so one blob cannot be both. The
manifest stays at that exact URL -- a consumer already polling it keeps working -- and carries
`artifact_url` pointing at the workbook beside it.

`status.json` is written TWICE: `running` before the pipeline starts, then the terminal state.
Without the first write, a job that is killed mid-run (OOM, node eviction, timeout) leaves no
trace at all and looks identical to one that was never submitted.


EXIT CODES
----------
    0  succeeded
    1  pipeline or upload failed   -- status blob says "failed"
    2  bad job payload             -- nothing was attempted, no status blob written
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from aria_pipeline import (                                          # noqa: E402
    InputError, classify_inputs, run_pipeline,
)
from blob_io import (                                                # noqa: E402
    BlobAccessError, BlobConfig, BlobStore, blob_filename, sibling_url,
)

log = logging.getLogger("aria.job")

REQUIRED_FIELDS = ("job_id", "input_urls", "output_url", "email_url")

EXIT_OK, EXIT_FAILED, EXIT_BAD_PAYLOAD = 0, 1, 2


def setup_logging() -> None:
    """One line per event, job_id included by the caller. LOG_LEVEL overrides.

    Logs go to stdout so a container runtime collects them; only the process's own crash
    output goes to stderr.
    """
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )
    # The SDK logs one line per HTTP request at INFO, and at DEBUG it logs headers --
    # including the Authorization header. Pinned to WARNING so a raised LOG_LEVEL cannot
    # leak a credential into the job log.
    for noisy in ("azure", "azure.core.pipeline.policies.http_logging_policy",
                  "azure.identity", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def load_job(path: Path | None) -> dict:
    """Read the payload from a file or stdin, and validate its shape.

    Validation happens before any network call so a malformed payload costs nothing and
    cannot half-run.
    """
    raw = path.read_text() if path else sys.stdin.read()
    if not raw.strip():
        raise ValueError("empty job payload")
    try:
        job = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"job payload is not valid JSON: {exc}") from exc
    if not isinstance(job, dict):
        raise ValueError(f"job payload must be a JSON object, got {type(job).__name__}")

    missing = [f for f in REQUIRED_FIELDS if not job.get(f)]
    if missing:
        raise ValueError(f"job payload is missing required field(s): {missing}")
    if not isinstance(job["input_urls"], list):
        raise ValueError("input_urls must be a list")
    return job


def write_status(store: BlobStore, job: dict, status: str, **extra) -> None:
    """Write status.json. Never raises: a status write must not mask the real error."""
    payload = {"job_id": job["job_id"], "status": status, "updated_utc": utc_now(), **extra}
    try:
        store.upload_json(payload, job["email_url"])
    except Exception as exc:                                  # noqa: BLE001
        log.error("could not write status blob (%s): %s", status, exc)


def handle_job(job: dict, store: BlobStore, *, keep_work: bool = False) -> int:
    job_id = job["job_id"]
    # Per-job directory: concurrent workers on one node must not collide, and an aborted job
    # must not leave state that a later job picks up.
    work = Path(tempfile.mkdtemp(prefix=f"aria-{job_id}-"))
    log.info("job %s starting (auth: %s, work: %s)",
             job_id, store.credential_summary(), work)

    write_status(store, job, "running", started_utc=utc_now())

    try:
        # ---- download
        downloaded: list[Path] = []
        for url in job["input_urls"]:
            name = blob_filename(url)
            dest = work / "input" / name
            # .docx/.xlsx must be real ZIP containers; anything else is an error page.
            expect_zip = dest.suffix.lower() in (".docx", ".xlsx")
            store.download(url, dest, expect_zip=expect_zip)
            downloaded.append(dest)
        log.info("job %s downloaded %d input(s)", job_id, len(downloaded))

        # ---- identify
        inputs = classify_inputs(downloaded)

        # ---- extract
        result = run_pipeline(inputs, work)
        if not result.artifact.is_file():
            raise RuntimeError(
                f"pipeline returned {result.artifact.name} but the file does not exist")
        for warning in result.warnings:
            log.warning("job %s: %s", job_id, warning)

        # ---- upload the artifact, then the manifest
        artifact_url = sibling_url(job["output_url"], result.artifact.name)
        store.upload_file(result.artifact, artifact_url)

        manifest = {
            "job_id": job_id,
            "status": "succeeded",
            "generated_utc": utc_now(),
            "artifact_url": artifact_url,
            "inputs": [blob_filename(u) for u in job["input_urls"]],
            **result.summary(),
        }
        store.upload_json(manifest, job["output_url"])

        # Status last: it is the signal a watcher acts on, so it must not claim success
        # before the artifact and manifest are both durably written.
        write_status(store, job, "succeeded",
                     artifact_url=artifact_url,
                     needs_review=result.needs_review,
                     confidence=result.confidence)
        log.info("job %s succeeded -> %s", job_id, result.artifact.name)
        return EXIT_OK

    except (InputError, BlobAccessError) as exc:
        # Expected, diagnosable failures: bad inputs or storage trouble. The message is
        # written verbatim to the status blob because it is meant to be read by a person.
        log.error("job %s failed: %s", job_id, exc)
        write_status(store, job, "failed",
                     error_type=type(exc).__name__, detail=str(exc))
        return EXIT_FAILED

    except Exception as exc:                                  # noqa: BLE001
        # Unexpected: the traceback goes to the log, but only the message goes to the status
        # blob. A traceback can carry file paths and payload fragments, and that blob may be
        # read by people outside the team.
        log.exception("job %s failed unexpectedly", job_id)
        write_status(store, job, "failed",
                     error_type=type(exc).__name__, detail=str(exc))
        return EXIT_FAILED

    finally:
        if keep_work:
            log.info("--keep-work: leaving %s in place", work)
        else:
            # Guideline documents are not ours to redistribute and may sit on a shared node,
            # so input copies are removed on every path, including failure.
            shutil.rmtree(work, ignore_errors=True)


def check_only(job: dict, store: BlobStore) -> int:
    """Validate wiring without running the pipeline or writing the output blob.

    Confirms the payload, the credential, and that every input is readable -- the three
    things that break on a first deploy -- while writing nothing a consumer might act on.
    """
    log.info("check: job %s", job["job_id"])
    log.info("check: auth %s", store.credential_summary())
    ok = True

    # Reads are anonymous, so these succeed with no credential at all.
    for url in job["input_urls"]:
        try:
            props = store._client(url).get_blob_properties()
            log.info("check: readable  %s (%s bytes)",
                     blob_filename(url), f"{props.size:,}")
        except Exception as exc:                              # noqa: BLE001
            log.error("check: UNREADABLE %s -- %s", blob_filename(url), exc)
            ok = False

    # Writes need the account key. Asking with write=True surfaces a missing key HERE,
    # during a check that writes nothing, instead of two minutes into a real job after the
    # extraction has already run.
    if not store.can_write():
        log.error("check: NO WRITE CREDENTIAL -- reads work, but every job will fail when it "
                  "uploads its result. Set AZURE_STORAGE_KEY.")
        ok = False
    else:
        for field in ("output_url", "email_url"):
            try:
                store._client(job[field], write=True)
                log.info("check: writable target accepted for %s", field)
            except Exception as exc:                          # noqa: BLE001
                log.error("check: %s rejected -- %s", field, exc)
                ok = False

    log.info("check: %s", "OK" if ok else "PROBLEMS FOUND")
    return EXIT_OK if ok else EXIT_FAILED


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--job", type=Path,
                    help="job payload JSON file (default: read stdin)")
    ap.add_argument("--check", action="store_true",
                    help="validate payload, credential and input readability, then stop")
    ap.add_argument("--keep-work", action="store_true",
                    help="keep the per-job temp dir for debugging")
    ap.add_argument("--env-file", type=Path,
                    help="load environment from this file (local development only)")
    args = ap.parse_args(argv)

    setup_logging()

    # Local convenience only. In Azure the environment comes from the container's app
    # settings and the credential from managed identity, so no env file is involved.
    if args.env_file:
        if not args.env_file.is_file():
            log.error("no such env file: %s", args.env_file)
            return EXIT_BAD_PAYLOAD
        try:
            from dotenv import load_dotenv
            load_dotenv(args.env_file, override=False)
            log.info("loaded environment from %s", args.env_file)
        except ImportError:
            log.error("--env-file needs python-dotenv (pip install python-dotenv)")
            return EXIT_BAD_PAYLOAD

    try:
        job = load_job(args.job)
    except ValueError as exc:
        log.error("%s", exc)
        return EXIT_BAD_PAYLOAD

    try:
        store = BlobStore(BlobConfig.from_env())
    except BlobAccessError as exc:
        # No status blob here: without a credential there is nowhere to write one.
        log.error("%s", exc)
        return EXIT_BAD_PAYLOAD

    if args.check:
        return check_only(job, store)
    return handle_job(job, store, keep_work=args.keep_work)


if __name__ == "__main__":
    sys.exit(main())
