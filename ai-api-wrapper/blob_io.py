"""Azure Blob Storage access: credential resolution, download, upload.

The only module here that touches the network. Nothing in it is ARIA-specific, so it should
not need editing when the AI pipeline is added.

READS ARE ANONYMOUS, WRITES NEED THE ACCOUNT KEY
------------------------------------------------
This is a proof of concept. Managed identity will not be granted, so the deployment model is:

    download  ->  no credential at all. Both containers allow anonymous read.
    upload    ->  AZURE_STORAGE_KEY required.

Verified against the live account:

    GET  ariallminput/...    HTTP 200     anonymous
    GET  ariallmoutput/...   HTTP 200     anonymous
    PUT  ariallmoutput/...   HTTP 401     anonymous -> rejected

So a credential is fetched only when writing, and `download()` works with none. A missing key
therefore fails at the first UPLOAD with a clear message, rather than at startup — the service
can still start and serve read-only traffic.

CREDENTIAL ORDER
----------------
    1. AZURE_STORAGE_KEY        <- the POC path. Writes.
    2. AZURE_STORAGE_SAS_TOKEN  <- honoured if set, but not required
    3. DefaultAzureCredential   <- only when USE_MANAGED_IDENTITY=1

`USE_MANAGED_IDENTITY` now defaults to **0**. It was 1, which is the right production default,
but on a VM without an assigned identity the Azure SDK works through nine credential fallbacks
before failing — an ~80 second hang per operation that reads like a bug in this code. Since the
identity is not coming, defaulting to it only produces that hang. The code path is kept intact:
set `USE_MANAGED_IDENTITY=1` and this reverts to the production behaviour with no other change.

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


class WriteCredentialMissing(BlobAccessError):
    """An upload was attempted with no credential. Reads do not raise this."""


@dataclass(frozen=True)
class BlobConfig:
    account: str = ""
    sas_token: str = ""
    account_key: str = ""
    use_managed_identity: bool = False     # see the module docstring: default flipped for POC
    timeout_seconds: int = 300

    @classmethod
    def from_env(cls) -> "BlobConfig":
        return cls(
            account=os.environ.get("AZURE_STORAGE_ACCOUNT", "").strip(),
            sas_token=os.environ.get("AZURE_STORAGE_SAS_TOKEN", "").strip(),
            account_key=os.environ.get("AZURE_STORAGE_KEY", "").strip(),
            # Opt IN to managed identity, rather than opting out. "1" is the only value that
            # enables it, so a typo leaves the working POC path rather than the hanging one.
            use_managed_identity=os.environ.get("USE_MANAGED_IDENTITY", "0") == "1",
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
        """(credential, mechanism). Returns (None, 'anonymous') when nothing is configured.

        Deliberately does NOT raise. Anonymous is a valid, working state here -- both
        containers allow public read -- so refusing to construct would block the read path
        over a credential only the write path needs.
        """
        cfg = self.config
        if cfg.account_key:
            return cfg.account_key, "account key"
        if cfg.sas_token:
            return cfg.sas_token, "SAS token"
        if cfg.use_managed_identity:
            from azure.identity import DefaultAzureCredential
            return DefaultAzureCredential(), "DefaultAzureCredential"
        return None, "anonymous (read-only)"

    def credential_summary(self) -> str:
        """The mechanism in use. Never the secret."""
        return self._mechanism

    def can_write(self) -> bool:
        """True if uploads are possible. Anonymous access can read but not write."""
        return self._credential is not None

    def _client(self, url: str, *, write: bool = False) -> BlobClient:
        """Validate the URL, then build a client. `write=True` demands a credential.

        The account check matters because a key is scoped to one account: a URL naming a
        different account means the job payload and the environment disagree. Stopping here
        beats letting it fail as an auth error, which sends whoever is on call looking at
        permissions instead of at the payload.
        """
        host = urlparse(url).netloc
        named = host.split(".")[0] if host else ""
        if not named:
            raise BlobAccessError(f"not a blob URL: {url}")
        if self.config.account and named != self.config.account:
            raise BlobAccessError(
                f"blob URL account {named!r} does not match AZURE_STORAGE_ACCOUNT "
                f"{self.config.account!r}: {url}")
        if write and self._credential is None:
            # Raised before the request, not after a 401: the Azure error for an anonymous
            # write is "AuthenticationFailed", which reads like a wrong key rather than no key.
            raise WriteCredentialMissing(
                f"cannot write {url}\n"
                f"  Reads are anonymous, but uploads need AZURE_STORAGE_KEY.\n"
                f"  Set it in the environment or in ai-api-wrapper/.env")
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
            client = self._client(url, write=True)
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
            client = self._client(url, write=True)
            client.upload_blob(
                body, overwrite=True,
                content_settings=ContentSettings(content_type="application/json"),
                timeout=self.config.timeout_seconds)
        except AzureError as exc:
            raise BlobAccessError(f"upload failed for {url}: {exc}") from exc
        log.info("uploaded %s (%s bytes)", blob_filename(url), f"{len(body):,}")
