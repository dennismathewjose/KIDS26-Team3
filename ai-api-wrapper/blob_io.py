"""Azure Blob Storage access: credential resolution, download, upload.

The only module here that touches the network. Nothing in it is ARIA-specific, so it should
not need editing when the AI pipeline is added.

CREDENTIAL ORDER
----------------
    1. DefaultAzureCredential   <- production. Managed identity inside Azure.
    2. AZURE_STORAGE_SAS_TOKEN  <- scoped, expiring; preferred for local dev
    3. AZURE_STORAGE_KEY        <- full account access; local dev only, last resort

Managed identity is FIRST, not last. A deployed job should never carry a long-lived account
key: it cannot be rotated without a redeploy, it grants far more than one job needs, and it
ends up in env dumps and crash logs. `USE_MANAGED_IDENTITY=0` opts out for local runs.

No credential value is ever logged. `credential_summary()` returns the mechanism, not the
secret.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from azure.core.exceptions import AzureError
from azure.storage.blob import BlobClient, ContentSettings

log = logging.getLogger(__name__)

# A .docx / .xlsx is a ZIP container. An HTML error page or a JSON fault saved under an
# .xlsx name is a real failure mode when a URL 404s through a proxy that returns 200.
ZIP_MAGIC = b"PK\x03\x04"

XLSX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
CONTENT_TYPES = {".xlsx": XLSX_CONTENT_TYPE, ".docx": DOCX_CONTENT_TYPE,
                 ".json": "application/json", ".csv": "text/csv",
                 ".txt": "text/plain; charset=utf-8"}


class BlobAccessError(RuntimeError):
    """A download or upload failed. Carries the blob URL, never the credential."""


@dataclass(frozen=True)
class BlobConfig:
    account: str = ""
    sas_token: str = ""
    account_key: str = ""
    use_managed_identity: bool = True
    timeout_seconds: int = 300

    @classmethod
    def from_env(cls) -> "BlobConfig":
        return cls(
            account=os.environ.get("AZURE_STORAGE_ACCOUNT", "").strip(),
            sas_token=os.environ.get("AZURE_STORAGE_SAS_TOKEN", "").strip(),
            account_key=os.environ.get("AZURE_STORAGE_KEY", "").strip(),
            use_managed_identity=os.environ.get("USE_MANAGED_IDENTITY", "1") != "0",
            timeout_seconds=int(os.environ.get("BLOB_TIMEOUT_SECONDS", "300")),
        )


def blob_filename(url: str) -> str:
    """Last path segment, percent-decoded.

    Job URLs may encode the virtual directory separator, e.g. `<prefix>%2Ffile.docx`, so
    splitting the raw URL on '/' yields the whole prefix. Decode first, then split.
    """
    return unquote(urlparse(url).path).rstrip("/").rsplit("/", 1)[-1]


def sibling_url(url: str, filename: str) -> str:
    """Same virtual directory as `url`, different file name."""
    base, _, _ = url.rpartition("/")
    return f"{base}/{quote(filename)}"


class BlobStore:
    """Blob IO with the credential decision made once, at construction."""

    def __init__(self, config: BlobConfig | None = None):
        self.config = config or BlobConfig.from_env()
        self._credential, self._mechanism = self._resolve_credential()

    def _resolve_credential(self):
        cfg = self.config
        if cfg.use_managed_identity:
            from azure.identity import DefaultAzureCredential
            return DefaultAzureCredential(), "DefaultAzureCredential"
        if cfg.sas_token:
            return cfg.sas_token, "SAS token"
        if cfg.account_key:
            return cfg.account_key, "account key"
        raise BlobAccessError(
            "no credential available: USE_MANAGED_IDENTITY=0 but neither "
            "AZURE_STORAGE_SAS_TOKEN nor AZURE_STORAGE_KEY is set")

    def credential_summary(self) -> str:
        """The mechanism in use. Never the secret."""
        return self._mechanism

    def _client(self, url: str) -> BlobClient:
        """Validate the URL's account, then build a client.

        A key or SAS is scoped to one account, so a URL naming a different account means the
        job description and the environment disagree. That is a misconfiguration to stop on,
        not something to attempt and let fail as an auth error -- the auth error would send
        whoever is on call looking at permissions instead of at the job payload.
        """
        host = urlparse(url).netloc
        named = host.split(".")[0] if host else ""
        if not named:
            raise BlobAccessError(f"not a blob URL: {url}")
        if self.config.account and named != self.config.account:
            raise BlobAccessError(
                f"blob URL account {named!r} does not match AZURE_STORAGE_ACCOUNT "
                f"{self.config.account!r}: {url}")
        return BlobClient.from_blob_url(url, credential=self._credential)

    # ------------------------------------------------------------------ download

    def download(self, url: str, dest: Path, *, expect_zip: bool = False) -> int:
        """Download to `dest`. Returns the byte count.

        Always binary: .docx and .xlsx are ZIP containers and any text-mode handling
        (encoding, newline translation) corrupts them irrecoverably.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            client = self._client(url)
            with open(dest, "wb") as fh:
                stream = client.download_blob(timeout=self.config.timeout_seconds)
                stream.readinto(fh)
        except AzureError as exc:
            raise BlobAccessError(f"download failed for {url}: {exc}") from exc

        size = dest.stat().st_size
        if size == 0:
            raise BlobAccessError(f"downloaded 0 bytes from {url}")
        if expect_zip:
            with open(dest, "rb") as fh:
                if fh.read(4) != ZIP_MAGIC:
                    raise BlobAccessError(
                        f"{dest.name} is not a valid .docx/.xlsx (no ZIP header). "
                        f"A proxy or SAS error page may have been saved in its place: {url}")
        log.info("downloaded %s (%s bytes)", dest.name, f"{size:,}")
        return size

    # -------------------------------------------------------------------- upload

    def upload_file(self, path: Path, url: str, *, content_type: str | None = None) -> None:
        """Upload a file, overwriting. Content type is inferred from the suffix if omitted.

        Setting it matters: without it Azure serves the blob as
        application/octet-stream, so a browser downloads the workbook as an unnamed binary
        instead of opening it.
        """
        ctype = content_type or CONTENT_TYPES.get(path.suffix.lower())
        try:
            client = self._client(url)
            with open(path, "rb") as fh:
                client.upload_blob(
                    fh, overwrite=True,
                    content_settings=ContentSettings(content_type=ctype) if ctype else None,
                    timeout=self.config.timeout_seconds)
        except AzureError as exc:
            raise BlobAccessError(f"upload failed for {url}: {exc}") from exc
        log.info("uploaded %s (%s bytes)", path.name, f"{path.stat().st_size:,}")

    def upload_json(self, payload: dict, url: str) -> None:
        import json
        body = json.dumps(payload, indent=2, default=str).encode("utf-8")
        try:
            client = self._client(url)
            client.upload_blob(
                body, overwrite=True,
                content_settings=ContentSettings(content_type="application/json"),
                timeout=self.config.timeout_seconds)
        except AzureError as exc:
            raise BlobAccessError(f"upload failed for {url}: {exc}") from exc
        log.info("uploaded %s (%s bytes)", blob_filename(url), f"{len(body):,}")
