"""Artifact storage: where rendered episode audio ends up, behind a Protocol
so the pipeline never writes files itself (Phase B — Vercel's filesystem is
ephemeral; `artifacts/` on disk does not survive past the response).

`LocalArtifactStore` is today's behavior (write to `artifacts/`).
`VercelBlobStore` uploads to Vercel Blob over its plain HTTP API (httpx, no
SDK — same "no heavy deps" convention as chorus/audio.py's direct ElevenLabs
call).

R5 (docs/REVIEW_WAVE1.md #5): audio must never be reachable by a bare,
unauthenticated URL. `Job.audio_url` is always the RELATIVE path
`/artifacts/<name>`; `chorus.app`'s `GET /artifacts/{name}` route (not a
StaticFiles mount) resolves the owning job from `name`, checks the caller's
owner against it, and only then calls `ArtifactStore.get(name)` to stream the
bytes back — an unauthenticated or wrong-owner request never reaches the
store at all, local or Blob. `VercelBlobStore` additionally uploads with
`x-vercel-blob-access: private` (confirmed available — verified 21 Sep 2026
against github.com/vercel/storage, packages/blob/src/put-helpers.ts's access
validation, which accepts exactly "private" or "public" — and against
vercel.com/docs/vercel-blob/private-storage's documented direct-curl pattern
for reading a private object with a bearer token), so even someone who
obtained the raw Blob URL cannot download it without our own
`BLOB_READ_WRITE_TOKEN`.

Vercel Blob HTTP API, verified 21 Sep 2026 against the `@vercel/blob` SDK
source (github.com/vercel/storage, packages/blob/src/{api.ts,put-helpers.ts} —
the public REST surface is not separately documented at vercel.com/docs, the
docs there point to the SDK/CLI instead):

    PUT https://blob.vercel-storage.com/<pathname>
    Headers:
      authorization: Bearer <BLOB_READ_WRITE_TOKEN>
      x-api-version: 12                 # SDK's current protocol version
      x-content-type: <mime type>       # e.g. audio/mpeg
      x-add-random-suffix: 0            # disables the random suffix so
                                         # artifact_stem's job_id-derived name
                                         # is the actual pathname, and so a
                                         # later `get(name)` can reconstruct
                                         # the same pathname deterministically
      x-vercel-blob-access: private     # R5: never "public" — see above
    Body: raw bytes
    Response 200 JSON (PutBlobResult): {"url", "downloadUrl", "pathname",
      "contentType", "contentDisposition", "etag", ...}

For a private store, `url` is `https://<store-id>.private.blob.vercel-
storage.com/<pathname>` and requires `Authorization: Bearer
<BLOB_READ_WRITE_TOKEN>` (or a short-lived OIDC token, not used here — the
low-level HTTP approach only has the long-lived token) on every GET, exactly
the pattern vercel.com/docs/vercel-blob/private-storage documents for
"Accessing without the SDK". Reconstructing that URL for `get(name)` without
re-uploading needs `<store-id>`, which Vercel sets as the `BLOB_STORE_ID` env
var on any project a Blob store is connected to (same doc, "Connect your
store to a project so Vercel adds ... BLOB_STORE_ID to your environment
automatically") — `VercelBlobStore` reads that env var directly rather than
depending on the SDK. When `BLOB_STORE_ID` is unset (a manually-configured
token whose store was never connected to this project) and no `put()` for
`name` has happened yet in this process, `get()` cannot resolve a URL and
returns None (serves as 404, never as an accidental public fetch) — a
documented, fails-closed residual limitation of the no-SDK approach, not a
public-exposure one.
"""
from __future__ import annotations

import logging
import mimetypes
import os
import re
from pathlib import Path
from typing import Protocol, runtime_checkable

import httpx

log = logging.getLogger("chorus.artifacts")

ARTIFACT_DIR = Path(__file__).resolve().parent.parent / "artifacts"

VERCEL_BLOB_BASE_URL = "https://blob.vercel-storage.com"
VERCEL_BLOB_API_VERSION = "12"
UPLOAD_TIMEOUT_S = 120.0
DOWNLOAD_TIMEOUT_S = 60.0
# Set by Vercel automatically on any project a Blob store is connected to
# (vercel.com/docs/vercel-blob/private-storage, "Creating a private Blob
# store"). Used to reconstruct a private blob's download URL for `get()`
# without needing the SDK or a durable name->url mapping of our own.
BLOB_STORE_ID_ENV = "BLOB_STORE_ID"
DEFAULT_CONTENT_TYPE = "application/octet-stream"

# `episode_<job_id>.<ext>` — chorus.pipeline.stage_audio's naming convention
# (artifact_stem below). job_id is uuid4().hex today (32 lowercase hex chars,
# no separators) but the pattern is kept permissive (hyphen/underscore too)
# to match artifact_stem's own validation rather than over-fitting to uuid4.
# An optional `__ask_<hex>` part names a voiced answer to a question about the
# job (chorus/ask.py); it belongs to the same job, so the same owner check holds.
_ARTIFACT_NAME_RE = re.compile(
    r"^episode_(?P<job_id>[A-Za-z0-9_-]+?)(?:__ask_[0-9a-f]{8,40})?\.(?P<ext>[A-Za-z0-9]+)$"
)


def artifact_stem(job_id: str) -> str:
    """Artifacts are keyed on the JOB, not the soul: two jobs with the same
    soul but different episodes must not overwrite each other's audio."""
    if not job_id or not job_id.replace("-", "").replace("_", "").isalnum():
        raise ValueError(f"job_id is not filename-safe: {job_id!r}")
    return f"episode_{job_id}"


def job_id_from_artifact_name(name: str) -> str | None:
    """The inverse of `artifact_stem` + extension: `chorus.app`'s
    `GET /artifacts/{name}` route uses this to resolve the owning job WITHOUT
    a separate name->job_id table (R5: "the job id is in the name")."""
    match = _ARTIFACT_NAME_RE.match(name)
    return match.group("job_id") if match else None


@runtime_checkable
class ArtifactStore(Protocol):
    def put(self, name: str, data: bytes, content_type: str) -> str:
        """Store `data` under `name` and return the URL a caller downloads
        it from — always the RELATIVE `/artifacts/<name>` (R5): the actual
        bytes are only ever served through chorus.app's owner-checked route,
        never a bare storage-provider URL."""
        ...

    def get(self, name: str) -> tuple[bytes, str] | None:
        """Fetch `name`'s bytes and content type for `chorus.app`'s
        `/artifacts/{name}` route to stream back, or None if unknown/
        unresolvable. Called only AFTER that route has already verified the
        caller owns the job `name` belongs to."""
        ...


class LocalArtifactStore:
    """Writes the file to local disk under `directory`. Local dev default."""

    def __init__(self, directory: Path = ARTIFACT_DIR) -> None:
        self.directory = directory

    def put(self, name: str, data: bytes, content_type: str) -> str:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / name
        path.write_bytes(data)
        log.info("artifacts(local): wrote %s (%d bytes)", path, len(data))
        return f"/artifacts/{name}"

    def get(self, name: str) -> tuple[bytes, str] | None:
        path = self.directory / name
        if not path.is_file():
            return None
        content_type = mimetypes.guess_type(name)[0] or DEFAULT_CONTENT_TYPE
        return path.read_bytes(), content_type


class VercelBlobStore:
    """Uploads to Vercel Blob. Activated when BLOB_READ_WRITE_TOKEN is set
    (see chorus/config_env.py). See the module docstring for the private-
    access design (R5)."""

    def __init__(
        self,
        token: str,
        base_url: str = VERCEL_BLOB_BASE_URL,
        store_id: str | None = None,
    ) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.store_id = store_id if store_id is not None else os.environ.get(BLOB_STORE_ID_ENV)
        # Same-process shortcut only (a fresh instance after a cold start
        # starts empty) — get() falls back to `_private_url` via
        # `BLOB_STORE_ID` when a name isn't here, so correctness never
        # depends on this cache surviving a restart.
        self._urls: dict[str, str] = {}

    def put(self, name: str, data: bytes, content_type: str) -> str:
        resp = httpx.put(
            f"{self.base_url}/{name}",
            headers={
                "authorization": f"Bearer {self.token}",
                "x-api-version": VERCEL_BLOB_API_VERSION,
                "x-content-type": content_type,
                "x-add-random-suffix": "0",
                "x-vercel-blob-access": "private",
            },
            content=data,
            timeout=UPLOAD_TIMEOUT_S,
        )
        resp.raise_for_status()
        body = resp.json()
        url = str(body["url"])
        self._urls[name] = url
        log.info("artifacts(blob): wrote %s (%d bytes, private)", url, len(data))
        # R5: never hand back the raw Blob URL — chorus.app always exposes
        # audio at the relative, owner-checked /artifacts/<name> route.
        return f"/artifacts/{name}"

    def _private_url(self, name: str) -> str | None:
        if not self.store_id:
            return None
        return f"https://{self.store_id}.private.blob.vercel-storage.com/{name}"

    def get(self, name: str) -> tuple[bytes, str] | None:
        url = self._urls.get(name) or self._private_url(name)
        if url is None:
            log.error(
                "artifacts(blob): cannot resolve a download URL for %s "
                "(BLOB_STORE_ID is unset and this process never uploaded it)",
                name,
            )
            return None
        resp = httpx.get(
            url,
            headers={"authorization": f"Bearer {self.token}"},
            timeout=DOWNLOAD_TIMEOUT_S,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        content_type = resp.headers.get("content-type") or DEFAULT_CONTENT_TYPE
        return resp.content, content_type
