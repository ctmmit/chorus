"""Artifact storage: where rendered episode audio ends up, behind a Protocol
so the pipeline never writes files itself (Phase B — Vercel's filesystem is
ephemeral; `artifacts/` on disk does not survive past the response).

`LocalArtifactStore` is today's behavior (write to `artifacts/`, served by
StaticFiles at `/artifacts/<name>`). `VercelBlobStore` uploads to Vercel Blob
over its plain HTTP API (httpx, no SDK — same "no heavy deps" convention as
chorus/audio.py's direct ElevenLabs call).

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
                                         # is the actual pathname (matches
                                         # LocalArtifactStore's behavior)
      x-vercel-blob-access: public      # mandatory; private needs signed URLs,
                                         # out of scope here
    Body: raw bytes
    Response 200 JSON (PutBlobResult): {"url", "downloadUrl", "pathname",
      "contentType", "contentDisposition", "etag", ...}

We return `url` (not `downloadUrl`): `downloadUrl` forces
`Content-Disposition: attachment`, which fights episode playback; `url` is
the same "GET it and you get the bytes" contract LocalArtifactStore offers.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol, runtime_checkable

import httpx

log = logging.getLogger("chorus.artifacts")

ARTIFACT_DIR = Path(__file__).resolve().parent.parent / "artifacts"

VERCEL_BLOB_BASE_URL = "https://blob.vercel-storage.com"
VERCEL_BLOB_API_VERSION = "12"
UPLOAD_TIMEOUT_S = 120.0


def artifact_stem(job_id: str) -> str:
    """Artifacts are keyed on the JOB, not the soul: two jobs with the same
    soul but different episodes must not overwrite each other's audio."""
    if not job_id or not job_id.replace("-", "").replace("_", "").isalnum():
        raise ValueError(f"job_id is not filename-safe: {job_id!r}")
    return f"episode_{job_id}"


@runtime_checkable
class ArtifactStore(Protocol):
    def put(self, name: str, data: bytes, content_type: str) -> str:
        """Store `data` under `name` and return the URL a caller downloads
        it from."""
        ...


class LocalArtifactStore:
    """Writes the file to local disk under `directory`, served by the
    `/artifacts` StaticFiles mount. Today's behavior, unchanged."""

    def __init__(self, directory: Path = ARTIFACT_DIR) -> None:
        self.directory = directory

    def put(self, name: str, data: bytes, content_type: str) -> str:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / name
        path.write_bytes(data)
        log.info("artifacts(local): wrote %s (%d bytes)", path, len(data))
        return f"/artifacts/{name}"


class VercelBlobStore:
    """Uploads to Vercel Blob. Activated when BLOB_READ_WRITE_TOKEN is set
    (see chorus/config_env.py)."""

    def __init__(self, token: str, base_url: str = VERCEL_BLOB_BASE_URL) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")

    def put(self, name: str, data: bytes, content_type: str) -> str:
        resp = httpx.put(
            f"{self.base_url}/{name}",
            headers={
                "authorization": f"Bearer {self.token}",
                "x-api-version": VERCEL_BLOB_API_VERSION,
                "x-content-type": content_type,
                "x-add-random-suffix": "0",
                "x-vercel-blob-access": "public",
            },
            content=data,
            timeout=UPLOAD_TIMEOUT_S,
        )
        resp.raise_for_status()
        body = resp.json()
        url = body["url"]
        log.info("artifacts(blob): wrote %s (%d bytes)", url, len(data))
        return str(url)
