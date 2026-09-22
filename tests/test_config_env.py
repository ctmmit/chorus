"""chorus/config_env.py: environment-driven backend selection, and
chorus/app.py's wiring of those choices (mounts, auth exemption)."""
from __future__ import annotations

from pathlib import Path

import inngest
import pytest
from fastapi.testclient import TestClient

from chorus import config_env
from chorus.app import create_app
from chorus.artifacts import ArtifactStore, LocalArtifactStore, VercelBlobStore
from chorus.audio import MockAudioRenderer
from chorus.jobs import SqliteJobStore
from chorus.llm import MockLLMClient
from chorus.pipeline import Deps
from chorus.runners import BackgroundRunner, InngestRunner
from chorus.script import MockScriptComposer
from chorus.transcripts import FixtureTranscriptProvider


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in (
        "DATABASE_URL",
        "BLOB_READ_WRITE_TOKEN",
        "INNGEST_EVENT_KEY",
        "INNGEST_SIGNING_KEY",
        "ANTHROPIC_API_KEY",
        "ELEVENLABS_API_KEY",
        "CHORUS_API_TOKEN",
    ):
        monkeypatch.delenv(k, raising=False)


def test_select_job_store_defaults_to_sqlite() -> None:
    assert isinstance(config_env.select_job_store(), SqliteJobStore)


def test_select_artifact_store_defaults_to_local() -> None:
    assert isinstance(config_env.select_artifact_store(), LocalArtifactStore)


def test_select_artifact_store_uses_blob_when_token_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "tok")
    assert isinstance(config_env.select_artifact_store(), VercelBlobStore)


def test_inngest_configured_requires_both_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    assert config_env.inngest_configured() is False
    monkeypatch.setenv("INNGEST_EVENT_KEY", "ek")
    assert config_env.inngest_configured() is False
    monkeypatch.setenv("INNGEST_SIGNING_KEY", "sk")
    assert config_env.inngest_configured() is True


def _deps(tmp_path: Path, artifacts: ArtifactStore | None = None) -> Deps:
    return Deps(
        FixtureTranscriptProvider(),
        MockLLMClient(),
        MockScriptComposer(),
        MockAudioRenderer(),
        artifacts or LocalArtifactStore(tmp_path / "artifacts"),
    )


def test_select_runner_defaults_to_background(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    runner = config_env.select_runner(store, _deps(tmp_path))
    assert isinstance(runner, BackgroundRunner)


def test_select_runner_uses_inngest_when_keys_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INNGEST_EVENT_KEY", "ek")
    monkeypatch.setenv("INNGEST_SIGNING_KEY", "sk")
    store = SqliteJobStore(tmp_path / "jobs.db")
    runner = config_env.select_runner(store, _deps(tmp_path))
    assert isinstance(runner, InngestRunner)


# --- create_app wiring ------------------------------------------------------


def test_get_artifacts_route_serves_owned_local_artifact(tmp_path: Path) -> None:
    # R5: no more StaticFiles mount — GET /artifacts/{name} resolves the
    # owning job from the name and serves through the artifact store.
    store = SqliteJobStore(tmp_path / "jobs.db")
    deps = _deps(tmp_path)
    job_id = store.create()
    name = f"episode_{job_id}.txt"
    deps.artifacts.put(name, b"hi", "text/plain")
    app = create_app(store, deps, runner=BackgroundRunner(store, deps))
    client = TestClient(app)
    assert client.get(f"/artifacts/{name}").text == "hi"


class _NotLocal:
    """A non-LocalArtifactStore ArtifactStore stand-in that implements `get`
    too — nothing local to serve (Vercel Blob content is fetched server-side
    through this same `.get()` contract instead)."""

    def __init__(self) -> None:
        self.stored: dict[str, bytes] = {}

    def put(self, name: str, data: bytes, content_type: str) -> str:
        self.stored[name] = data
        return f"/artifacts/{name}"

    def get(self, name: str) -> tuple[bytes, str] | None:
        data = self.stored.get(name)
        return (data, "text/plain") if data is not None else None


def test_artifacts_route_404s_for_unknown_name(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    deps = _deps(tmp_path, artifacts=_NotLocal())
    app = create_app(store, deps, runner=BackgroundRunner(store, deps))
    client = TestClient(app)
    assert client.get("/artifacts/anything").status_code == 404


def test_inngest_runner_mounts_api_inngest(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    deps = _deps(tmp_path)
    # signing_key: inngest's CommHandler requires one in "production" mode
    # (no dev-server env detected) even just to construct the route — real
    # signature verification only happens on incoming requests.
    client_stub = inngest.Inngest(app_id="chorus-test", signing_key="signkey_test")
    runner = InngestRunner(client_stub)
    app = create_app(store, deps, runner=runner)
    client = TestClient(app)
    # No CHORUS_API_TOKEN is set, so this would be open anyway; the point is
    # that the route exists at all (a mounted-elsewhere app would 404).
    assert client.get("/api/inngest").status_code != 404


def test_background_runner_does_not_mount_api_inngest(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    deps = _deps(tmp_path)
    app = create_app(store, deps, runner=BackgroundRunner(store, deps))
    client = TestClient(app)
    assert client.get("/api/inngest").status_code == 404


def test_api_inngest_exempt_from_bearer_auth(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    deps = _deps(tmp_path)
    client_stub = inngest.Inngest(app_id="chorus-test", signing_key="signkey_test")
    runner = InngestRunner(client_stub)
    app = create_app(store, deps, runner=runner, api_token="s3cret")
    client = TestClient(app)
    # No Authorization header at all — a normal route is 401 with OUR body.
    shows_resp = client.get("/shows")
    assert shows_resp.status_code == 401
    assert shows_resp.json()["detail"] == "missing or invalid bearer token"

    # /api/inngest never even reaches our bearer check (the middleware lets
    # it straight through) — Inngest's OWN signature check inside the route
    # then separately (and coincidentally) also 401s an unsigned GET, with a
    # different body, proving it's Inngest's check and not ours.
    inngest_resp = client.get("/api/inngest")
    assert inngest_resp.json() != shows_resp.json()
