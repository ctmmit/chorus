"""New-behavior tests for the Wave 1 remediation (docs/REVIEW_WAVE1.md),
findings R1-R25 except those owned by another concurrent agent
(chorus/transcripts.py, chorus/ingest.py, chorus/transcript_cache.py,
chorus/audio.py, chorus/script.py, the EpisodeInput class in
chorus/models.py) or the web/ viewer. One test module per finding id makes
it easy to map a failing test back to the finding it guards; existing tests
elsewhere (test_store_contract.py, test_inngest_app.py, test_llm_batch.py)
also gained coverage for R6/R7/R25 respectively, close to the code they
exercise.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient

from chorus import config_env
from chorus.app import PROVIDER_KEY_ENVS, create_app
from chorus.artifacts import VercelBlobStore
from chorus.audio import MockAudioRenderer
from chorus.email import MockEmailSender
from chorus.jobs import SqliteJobStore
from chorus.keys import SqliteKeyStore
from chorus.llm import MockLLMClient
from chorus.mcp_server import ChorusTools, MCPSubmitError
from chorus.models import TWO_HOST_PROFILE, DigestRequest, EpisodeInput, JobStatus
from chorus.pipeline import Deps
from chorus.script import MockScriptComposer
from chorus.transcripts import FixtureTranscriptProvider

FIX = Path(__file__).resolve().parent.parent / "fixtures"
ANDREESSEN = "c4tvVKDhpiY"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in (
        "DATABASE_URL",
        "BLOB_READ_WRITE_TOKEN",
        "BLOB_STORE_ID",
        "INNGEST_EVENT_KEY",
        "INNGEST_SIGNING_KEY",
        "ANTHROPIC_API_KEY",
        "ELEVENLABS_API_KEY",
        "TRANSCRIPT_API_KEY",
        "DEEPGRAM_API_KEY",
        "RESEND_API_KEY",
        "CHORUS_API_TOKEN",
        "CHORUS_ENV",
        "CHORUS_MAX_JOBS_PER_DAY",
        "CHORUS_MAX_INFLIGHT_JOBS",
        "CHORUS_KEY_ISSUE_PER_IP_PER_HOUR",
        "CHORUS_KEY_ALLOWED_EMAIL_DOMAINS",
        "CHORUS_CORS_ORIGINS",
        "CHORUS_STALE_JOB_SECONDS",
        "CHORUS_DB_PATH",
    ):
        monkeypatch.delenv(k, raising=False)


def _deps(tmp_path: Path, **overrides: object) -> Deps:
    kw: dict = {
        "provider": FixtureTranscriptProvider(),
        "llm": MockLLMClient(),
        "composer": MockScriptComposer(),
        "renderer": MockAudioRenderer(out_dir=tmp_path / "artifacts"),
    }
    kw.update(overrides)
    return Deps(**kw)


def _payload(*video_ids: str) -> dict:
    return {
        "soul": (FIX / "souls" / "soul_investor.md").read_text(encoding="utf-8"),
        "context": "",
        "episodes": [{"video_id": v} for v in video_ids],
        "highlight_count": 4,
    }


class _NoopRunner:
    """Never actually runs a job — used to keep a job `queued` forever so a
    concurrency (in-flight) quota can be exercised deterministically."""

    def submit(self, job_id: str, request: DigestRequest, background: BackgroundTasks | None = None) -> None:
        return None


class _RaisingRunner:
    """A JobRunner whose submit() always fails — R8."""

    def submit(self, job_id: str, request: DigestRequest, background: BackgroundTasks | None = None) -> None:
        raise RuntimeError("dispatch backend unavailable")


# --- R1: no repository-root SQLite construction in Postgres mode -----------


class _ExplodingSqlite:
    def __init__(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("a SQLite store was constructed while DATABASE_URL is set (R1)")


class _FakePgBase:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def close(self) -> None:
        pass


class _FakePgJobStore(_FakePgBase):
    def create(self, owner: str = "master") -> str:
        return "fake-job-id"

    def get(self, job_id: str):  # type: ignore[no-untyped-def]
        return None

    def save(self, job: object) -> None:
        pass

    def fail_in_flight(self, reason: str, older_than_seconds: float = 0) -> int:
        return 0

    def count_for_owner(self, owner: str, since: object) -> int:
        return 0

    def count_in_flight(self, owner: str) -> int:
        return 0


class _FakePgKeyStore(_FakePgBase):
    def issue(self, email: str) -> str:
        return "chorus_fake"

    def is_valid(self, token: str) -> bool:
        return False

    def revoke(self, token: str) -> None:
        pass

    def owner_of(self, token: str):  # type: ignore[no-untyped-def]
        return None


class _FakePgSubscriptionStore(_FakePgBase):
    def create(self, subscription: object) -> str:
        return "fake-sub-id"

    def get(self, subscription_id: str):  # type: ignore[no-untyped-def]
        return None

    def list(self, owner: str | None = None) -> list:  # type: ignore[type-arg]
        return []

    def save(self, subscription: object) -> None:
        pass

    def delete(self, subscription_id: str) -> None:
        pass

    def due(self, now: object) -> list:  # type: ignore[type-arg]
        return []


class _FakePgPersonaRegistry(_FakePgBase):
    def create(self, persona: object) -> object:
        return persona

    def get(self, persona_id: str):  # type: ignore[no-untyped-def]
        return None

    def list(self, *, public_only: bool = False) -> list:  # type: ignore[type-arg]
        return []

    def delete(self, persona_id: str) -> bool:
        return False


class _FakePgTranscriptCache(_FakePgBase):
    def get(self, episode_id: str):  # type: ignore[no-untyped-def]
        return None

    def put(self, transcript: object) -> None:
        pass


def test_r1_postgres_mode_never_constructs_a_sqlite_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")

    # Every Sqlite* class the selection modules could reach for: raise if
    # instantiated at all.
    monkeypatch.setattr(config_env, "SqliteJobStore", _ExplodingSqlite)
    monkeypatch.setattr("chorus.keys.SqliteKeyStore", _ExplodingSqlite)
    monkeypatch.setattr("chorus.subscriptions.SqliteSubscriptionStore", _ExplodingSqlite)
    monkeypatch.setattr("chorus.personas.SqlitePersonaRegistry", _ExplodingSqlite)
    monkeypatch.setattr("chorus.transcript_cache.SqliteTranscriptCache", _ExplodingSqlite)

    # Stub every Postgres class so nothing here needs a real database.
    monkeypatch.setattr("chorus.stores.postgres.PostgresJobStore", _FakePgJobStore)
    monkeypatch.setattr("chorus.stores.postgres.PostgresKeyStore", _FakePgKeyStore)
    monkeypatch.setattr("chorus.stores.postgres.PostgresSubscriptionStore", _FakePgSubscriptionStore)
    monkeypatch.setattr("chorus.stores.postgres.PostgresPersonaRegistry", _FakePgPersonaRegistry)
    monkeypatch.setattr("chorus.stores.postgres.PostgresTranscriptCache", _FakePgTranscriptCache)

    # build_deps must build the transcript chain directly, never via
    # default_deps() + a post-hoc cache swap.
    deps = config_env.build_deps()
    assert isinstance(deps.provider.cache, _FakePgTranscriptCache)  # type: ignore[attr-defined]

    store = config_env.select_job_store()
    assert isinstance(store, _FakePgJobStore)
    assert isinstance(config_env.select_key_store(store), _FakePgKeyStore)
    assert isinstance(config_env.select_subscription_store(store), _FakePgSubscriptionStore)
    assert isinstance(config_env.select_persona_registry(store), _FakePgPersonaRegistry)

    # And the full create_app() wiring, end to end, raises nothing either.
    app = create_app()
    assert isinstance(app.state.personas, _FakePgPersonaRegistry)


def test_chorus_db_path_env_overrides_default_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import chorus.jobs as jobs_module

    override = tmp_path / "custom.db"
    monkeypatch.setenv("CHORUS_DB_PATH", str(override))
    assert jobs_module._default_db() == override
    monkeypatch.delenv("CHORUS_DB_PATH", raising=False)
    assert jobs_module._default_db() == jobs_module._REPO_ROOT_DB


# --- R2: every spend-capable credential is a provider key ------------------


def test_r2_provider_key_envs_include_every_spend_capable_credential() -> None:
    assert set(PROVIDER_KEY_ENVS) >= {
        "ANTHROPIC_API_KEY",
        "ELEVENLABS_API_KEY",
        "TRANSCRIPT_API_KEY",
        "DEEPGRAM_API_KEY",
        "RESEND_API_KEY",
        "BLOB_READ_WRITE_TOKEN",
        "INNGEST_EVENT_KEY",
    }


@pytest.mark.parametrize("key_env", PROVIDER_KEY_ENVS)
def test_r2_any_provider_key_without_token_refuses_startup(
    key_env: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(key_env, "some-secret-value")
    store = SqliteJobStore(tmp_path / "jobs.db")
    with pytest.raises(RuntimeError, match=r"CHORUS_API_TOKEN"):
        with TestClient(create_app(store, _deps(tmp_path))):
            pass


def test_r2_production_env_refuses_startup_with_no_provider_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHORUS_ENV", "production")
    store = SqliteJobStore(tmp_path / "jobs.db")
    with pytest.raises(RuntimeError, match=r"CHORUS_API_TOKEN"):
        with TestClient(create_app(store, _deps(tmp_path))):
            pass


def test_r2_production_env_with_token_starts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHORUS_ENV", "production")
    store = SqliteJobStore(tmp_path / "jobs.db")
    with TestClient(create_app(store, _deps(tmp_path), api_token="s3cret")):
        pass  # must not raise


def test_r2_no_provider_keys_and_no_production_env_starts_open(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    with TestClient(create_app(store, _deps(tmp_path))):
        pass  # must not raise — matches today's local/dev/mock-provider behavior


# --- R3: per-owner spend quotas + key-issuance throttling -------------------


def test_r3_daily_job_quota_returns_429_once_exceeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHORUS_MAX_JOBS_PER_DAY", "2")
    store = SqliteJobStore(tmp_path / "jobs.db")
    key_store = SqliteKeyStore(tmp_path / "jobs.db")
    token = key_store.issue("quota@example.com")
    app = create_app(store, _deps(tmp_path), api_token="master-token", key_store=key_store)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}

    assert client.post("/digest", json=_payload(ANDREESSEN), headers=headers).status_code == 200
    assert client.post("/digest", json=_payload(ANDREESSEN), headers=headers).status_code == 200
    third = client.post("/digest", json=_payload(ANDREESSEN), headers=headers)
    assert third.status_code == 429
    assert "24h" in third.json()["detail"]


def test_r3_master_owner_is_exempt_from_daily_quota(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHORUS_MAX_JOBS_PER_DAY", "1")
    store = SqliteJobStore(tmp_path / "jobs.db")
    app = create_app(store, _deps(tmp_path), api_token="master-token")
    client = TestClient(app)
    headers = {"Authorization": "Bearer master-token"}

    for _ in range(3):
        assert client.post("/digest", json=_payload(ANDREESSEN), headers=headers).status_code == 200


def test_r3_inflight_quota_returns_429_once_exceeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHORUS_MAX_INFLIGHT_JOBS", "1")
    store = SqliteJobStore(tmp_path / "jobs.db")
    key_store = SqliteKeyStore(tmp_path / "jobs.db")
    token = key_store.issue("inflight@example.com")
    # _NoopRunner never finishes a job, so the first submission stays queued
    # (in flight) for the rest of the test.
    app = create_app(store, _deps(tmp_path), api_token="master-token", key_store=key_store, runner=_NoopRunner())
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}

    first = client.post("/digest", json=_payload(ANDREESSEN), headers=headers)
    assert first.status_code == 200
    second = client.post("/digest", json=_payload(ANDREESSEN), headers=headers)
    assert second.status_code == 429
    assert "in flight" in second.json()["detail"]


def test_r3_digest_select_and_subscription_run_also_enforce_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHORUS_MAX_JOBS_PER_DAY", "1")
    store = SqliteJobStore(tmp_path / "jobs.db")
    key_store = SqliteKeyStore(tmp_path / "jobs.db")
    token = key_store.issue("select-quota@example.com")
    app = create_app(store, _deps(tmp_path), api_token="master-token", key_store=key_store)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}

    ok = client.post(
        "/digest/select",
        json={"soul": "# s", "context": "", "video_ids": [ANDREESSEN]},
        headers=headers,
    )
    assert ok.status_code == 200
    blocked = client.post(
        "/digest/select",
        json={"soul": "# s", "context": "", "video_ids": [ANDREESSEN]},
        headers=headers,
    )
    assert blocked.status_code == 429


def test_r3_key_issue_ip_throttle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHORUS_KEY_ISSUE_PER_IP_PER_HOUR", "2")
    store = SqliteJobStore(tmp_path / "jobs.db")
    sender = MockEmailSender()
    app = create_app(store, _deps(tmp_path), api_token="master-token", email_sender=sender)
    client = TestClient(app)

    assert client.post("/keys", json={"email": "a@example.com"}).status_code == 202
    assert client.post("/keys", json={"email": "b@example.com"}).status_code == 202
    third = client.post("/keys", json={"email": "c@example.com"})
    assert third.status_code == 429


def test_r3_key_issue_email_domain_allowlist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHORUS_KEY_ALLOWED_EMAIL_DOMAINS", "allowed.example")
    store = SqliteJobStore(tmp_path / "jobs.db")
    sender = MockEmailSender()
    app = create_app(store, _deps(tmp_path), api_token="master-token", email_sender=sender)
    client = TestClient(app)

    denied = client.post("/keys", json={"email": "person@other.example"})
    assert denied.status_code == 403
    allowed = client.post("/keys", json={"email": "person@allowed.example"})
    assert allowed.status_code == 202


class _RaisingEmailSender:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, to, subject, text, html=None, headers=None):  # type: ignore[no-untyped-def]
        self.calls += 1
        raise RuntimeError("resend is down")


def test_r23_email_failure_after_issuance_revokes_key_and_returns_502(
    tmp_path: Path,
) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    key_store = SqliteKeyStore(tmp_path / "jobs.db")
    sender = _RaisingEmailSender()
    app = create_app(
        store, _deps(tmp_path), api_token="master-token", key_store=key_store, email_sender=sender
    )
    client = TestClient(app)

    first = client.post("/keys", json={"email": "unlucky@example.com"})
    assert first.status_code == 502
    assert "revoked" in first.json()["detail"]
    assert sender.calls == 1

    # R23: the revoked attempt must NOT count toward the per-email hourly
    # limit — a retry with the same email must be allowed to try again
    # immediately (it will fail the same way here since the sender always
    # raises, but it must reach `issue` again rather than being rate-limited
    # first).
    second = client.post("/keys", json={"email": "unlucky@example.com"})
    assert second.status_code == 502
    assert sender.calls == 2


# --- R4: jobs have owners ----------------------------------------------


def _two_principal_app(tmp_path: Path) -> tuple[TestClient, dict[str, str], dict[str, str]]:
    store = SqliteJobStore(tmp_path / "jobs.db")
    key_store = SqliteKeyStore(tmp_path / "jobs.db")
    token_a = key_store.issue("alice@example.com")
    token_b = key_store.issue("bob@example.com")
    app = create_app(store, _deps(tmp_path), api_token="master-token", key_store=key_store)
    client = TestClient(app)
    return client, {"Authorization": f"Bearer {token_a}"}, {"Authorization": f"Bearer {token_b}"}


def test_r4_two_principal_job_isolation(tmp_path: Path) -> None:
    client, headers_a, headers_b = _two_principal_app(tmp_path)

    job_id = client.post("/digest", json=_payload(ANDREESSEN), headers=headers_a).json()["job_id"]

    assert client.get(f"/digest/{job_id}", headers=headers_a).status_code == 200
    foreign = client.get(f"/digest/{job_id}", headers=headers_b)
    assert foreign.status_code == 404  # never 403 — no ownership-enumeration signal
    master = client.get(f"/digest/{job_id}", headers={"Authorization": "Bearer master-token"})
    assert master.status_code == 200


def test_r4_subscription_run_now_job_is_owned_by_the_subscription_not_the_caller(
    tmp_path: Path,
) -> None:
    # The master token may run-now someone else's subscription; the
    # resulting job must still belong to the SUBSCRIPTION's owner, not to
    # master — so only that owner (or master) can read it afterward.
    client, headers_a, headers_b = _two_principal_app(tmp_path)

    created = client.post(
        "/subscriptions",
        json={
            "email": "alice@example.com",
            "soul": "# s",
            "context": "",
            "episodes": [{"video_id": ANDREESSEN}],
        },
        headers=headers_a,
    )
    assert created.status_code == 200
    sub_id = created.json()["subscription_id"]

    ran = client.post(
        f"/subscriptions/{sub_id}/run", headers={"Authorization": "Bearer master-token"}
    )
    assert ran.status_code == 200
    job_id = ran.json()["job_id"]

    assert client.get(f"/digest/{job_id}", headers=headers_a).status_code == 200
    assert client.get(f"/digest/{job_id}", headers=headers_b).status_code == 404


def test_r4_job_owner_field_defaults_to_master_for_old_rows() -> None:
    from chorus.models import Job

    job = Job(job_id="x", status=JobStatus.queued)
    assert job.owner == "master"


# --- R5: no public blob URLs; artifacts served through an owner-checked route


def test_r5_audio_url_is_always_relative(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    app = create_app(store, _deps(tmp_path), api_token="master-token")
    client = TestClient(app)
    headers = {"Authorization": "Bearer master-token"}

    job_id = client.post("/digest", json=_payload(ANDREESSEN), headers=headers).json()["job_id"]
    body = client.get(f"/digest/{job_id}", headers=headers).json()
    assert body["audio_url"].startswith("/artifacts/")


def test_r5_artifact_route_404s_for_a_foreign_owner(tmp_path: Path) -> None:
    client, headers_a, headers_b = _two_principal_app(tmp_path)

    job_id = client.post("/digest", json=_payload(ANDREESSEN), headers=headers_a).json()["job_id"]
    body = client.get(f"/digest/{job_id}", headers=headers_a).json()
    audio_url = body["audio_url"]
    assert audio_url

    assert client.get(audio_url, headers=headers_a).status_code == 200
    assert client.get(audio_url, headers=headers_b).status_code == 404


class _RecordingHTTPXResponse:
    def __init__(self, status_code: int, json_body: dict | None = None, content: bytes = b"", headers: dict | None = None) -> None:
        self.status_code = status_code
        self._json = json_body or {}
        self.content = content
        self.headers = headers or {}

    def json(self) -> dict:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")


def test_r5_vercel_blob_store_uploads_private_and_returns_relative_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_put(url: str, headers: dict, content: bytes, timeout: float) -> _RecordingHTTPXResponse:
        captured["headers"] = headers
        return _RecordingHTTPXResponse(200, {"url": "https://store123.private.blob.vercel-storage.com/episode_x.mp3"})

    monkeypatch.setattr("httpx.put", fake_put)
    store = VercelBlobStore(token="tok", store_id="store123")

    url = store.put("episode_x.mp3", b"data", "audio/mpeg")

    assert url == "/artifacts/episode_x.mp3"  # R5: never the raw Blob URL
    assert captured["headers"]["x-vercel-blob-access"] == "private"


def test_r5_vercel_blob_store_get_fetches_with_bearer_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_put(url: str, headers: dict, content: bytes, timeout: float) -> _RecordingHTTPXResponse:
        return _RecordingHTTPXResponse(200, {"url": "https://store123.private.blob.vercel-storage.com/episode_x.mp3"})

    fetched: dict[str, object] = {}

    def fake_get(url: str, headers: dict, timeout: float) -> _RecordingHTTPXResponse:
        fetched["url"] = url
        fetched["headers"] = headers
        return _RecordingHTTPXResponse(200, content=b"bytes-out", headers={"content-type": "audio/mpeg"})

    monkeypatch.setattr("httpx.put", fake_put)
    monkeypatch.setattr("httpx.get", fake_get)
    store = VercelBlobStore(token="secret-token", store_id="store123")
    store.put("episode_x.mp3", b"data", "audio/mpeg")

    result = store.get("episode_x.mp3")

    assert result == (b"bytes-out", "audio/mpeg")
    assert fetched["headers"]["authorization"] == "Bearer secret-token"


def test_r5_vercel_blob_store_get_without_store_id_or_prior_put_fails_closed() -> None:
    # No BLOB_STORE_ID given and no put() happened in this process: get()
    # must return None (serves as 404), never guess/construct a public URL.
    store = VercelBlobStore(token="tok", store_id=None)
    assert store.get("episode_never_uploaded.mp3") is None


# --- R8: a runner submission failure never strands a job --------------------


def test_r8_http_dispatch_failure_marks_failed_and_returns_503(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    app = create_app(store, _deps(tmp_path), api_token="master-token", runner=_RaisingRunner())
    client = TestClient(app)
    headers = {"Authorization": "Bearer master-token"}

    resp = client.post("/digest", json=_payload(ANDREESSEN), headers=headers)

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "failed"
    assert body["job_id"]
    job = store.get(body["job_id"])
    assert job is not None
    assert job.status == JobStatus.failed
    assert "dispatch failed" in job.error


def test_r8_mcp_dispatch_failure_raises_tool_error_carrying_job_id(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    tools = ChorusTools(store, _deps(tmp_path), runner=_RaisingRunner())

    with pytest.raises(MCPSubmitError) as excinfo:
        tools.submit_digest(
            soul=(FIX / "souls" / "soul_investor.md").read_text(encoding="utf-8"),
            context="",
            episodes=[EpisodeInput(video_id=ANDREESSEN)],
        )

    job_id = excinfo.value.job_id
    assert job_id in str(excinfo.value)
    job = store.get(job_id)
    assert job is not None and job.status == JobStatus.failed


# --- R9: MCP submission goes through the runner, returns immediately -------


class _RecordingRunner:
    """Records what it was asked to submit but never actually runs
    anything — proves ChorusTools._submit dispatches through the injected
    JobRunner (R9) instead of calling chorus.pipeline.run_job inline, the
    way it did before this fix (which made a long pipeline run vulnerable to
    being cut short by the MCP request's own lifetime on a hosted/Inngest
    deployment)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, DigestRequest, BackgroundTasks | None]] = []

    def submit(self, job_id: str, request: DigestRequest, background: BackgroundTasks | None = None) -> None:
        self.calls.append((job_id, request, background))


def test_r9_mcp_submit_dispatches_through_the_injected_runner_and_returns_immediately(
    tmp_path: Path,
) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    runner = _RecordingRunner()
    tools = ChorusTools(store, _deps(tmp_path), runner=runner)

    submitted = tools.submit_digest(
        soul=(FIX / "souls" / "soul_investor.md").read_text(encoding="utf-8"),
        context="",
        episodes=[EpisodeInput(video_id=ANDREESSEN)],
    )

    assert len(runner.calls) == 1
    called_job_id, called_request, called_background = runner.calls[0]
    assert called_job_id == submitted["job_id"]
    assert called_request.episodes[0].video_id == ANDREESSEN
    assert called_background is None  # no per-request BackgroundTasks on the MCP path
    # The runner above never actually executes anything — if submission had
    # instead awaited run_job() inline (the pre-fix behavior), the job would
    # already be `done` here. It must still be `queued`.
    job = store.get(submitted["job_id"])
    assert job is not None and job.status == JobStatus.queued


def test_r9_background_runner_used_by_mcp_falls_back_to_daemon_thread(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    tools = ChorusTools(store, _deps(tmp_path))  # default runner = BackgroundRunner
    submitted = tools.submit_digest(
        soul=(FIX / "souls" / "soul_investor.md").read_text(encoding="utf-8"),
        context="",
        episodes=[EpisodeInput(video_id=ANDREESSEN)],
    )
    deadline = time.monotonic() + 5.0
    job = store.get(submitted["job_id"])
    while job.status in (JobStatus.queued, JobStatus.digest_ready) and time.monotonic() < deadline:
        time.sleep(0.02)
        job = store.get(submitted["job_id"])
    assert job.status == JobStatus.done


# --- R13: CORS ---------------------------------------------------------


def test_r13_cors_preflight_is_not_authenticated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHORUS_CORS_ORIGINS", "https://viewer.example.com")
    store = SqliteJobStore(tmp_path / "jobs.db")
    app = create_app(store, _deps(tmp_path), api_token="master-token")
    client = TestClient(app)

    resp = client.options(
        "/shows",
        headers={
            "Origin": "https://viewer.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert resp.status_code < 400
    assert resp.headers.get("access-control-allow-origin") == "https://viewer.example.com"


def test_r13_cors_cross_origin_get_carries_allow_origin_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHORUS_CORS_ORIGINS", "https://viewer.example.com")
    store = SqliteJobStore(tmp_path / "jobs.db")
    app = create_app(store, _deps(tmp_path), api_token="master-token")
    client = TestClient(app)

    resp = client.get(
        "/shows",
        headers={"Authorization": "Bearer master-token", "Origin": "https://viewer.example.com"},
    )

    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "https://viewer.example.com"


def test_r13_no_cors_origins_configured_means_no_cors_headers(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    app = create_app(store, _deps(tmp_path), api_token="master-token")
    client = TestClient(app)

    resp = client.get(
        "/shows",
        headers={"Authorization": "Bearer master-token", "Origin": "https://viewer.example.com"},
    )

    assert "access-control-allow-origin" not in resp.headers


# --- R17: profile is preserved end to end -----------------------------


def test_r17_digest_select_preserves_profile(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    app = create_app(store, _deps(tmp_path), api_token="master-token")
    client = TestClient(app)
    headers = {"Authorization": "Bearer master-token"}

    payload = {
        "soul": (FIX / "souls" / "soul_investor.md").read_text(encoding="utf-8"),
        "context": "",
        "video_ids": [ANDREESSEN],
        "profile": TWO_HOST_PROFILE.model_dump(),
    }
    job_id = client.post("/digest/select", json=payload, headers=headers).json()["job_id"]
    body = client.get(f"/digest/{job_id}", headers=headers).json()
    assert body["script"]["format"] == "dialogue"


def test_r17_mcp_submit_selection_preserves_profile(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    tools = ChorusTools(store, _deps(tmp_path))
    submitted = tools.submit_selection(
        soul=(FIX / "souls" / "soul_investor.md").read_text(encoding="utf-8"),
        context="",
        video_ids=[ANDREESSEN],
        profile=TWO_HOST_PROFILE,
    )
    deadline = time.monotonic() + 5.0
    job = store.get(submitted["job_id"])
    while job.status in (JobStatus.queued, JobStatus.digest_ready) and time.monotonic() < deadline:
        time.sleep(0.02)
        job = store.get(submitted["job_id"])
    assert job.script is not None and job.script.format == "dialogue"


def test_r17_mcp_submit_digest_accepts_profile(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    tools = ChorusTools(store, _deps(tmp_path))
    submitted = tools.submit_digest(
        soul=(FIX / "souls" / "soul_investor.md").read_text(encoding="utf-8"),
        context="",
        episodes=[EpisodeInput(video_id=ANDREESSEN)],
        profile=TWO_HOST_PROFILE,
    )
    deadline = time.monotonic() + 5.0
    job = store.get(submitted["job_id"])
    while job.status in (JobStatus.queued, JobStatus.digest_ready) and time.monotonic() < deadline:
        time.sleep(0.02)
        job = store.get(submitted["job_id"])
    assert job.script is not None and job.script.format == "dialogue"


# --- R22: streaming body-size limit without Content-Length -----------------


def test_r22_chunked_body_without_content_length_is_rejected_at_413() -> None:
    import asyncio

    from chorus.app import MAX_BODY_BYTES, GuardsMiddleware

    class _FakeKeyStore:
        def is_valid(self, token: str) -> bool:
            return False

        def owner_of(self, token: str) -> str | None:
            return None

    sent: list[dict] = []

    async def app(scope, receive, send):  # type: ignore[no-untyped-def]
        # A well-behaved inner app reads the whole body before responding —
        # exactly what triggers the wrapped receive() to raise partway
        # through a too-large chunked stream.
        more_body = True
        while more_body:
            message = await receive()
            more_body = message.get("more_body", False)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = GuardsMiddleware(app, token=None, key_store=_FakeKeyStore())

    chunk = b"x" * (MAX_BODY_BYTES // 4)
    chunks = [chunk] * 6  # 1.5x MAX_BODY_BYTES, no content-length header at all
    sent_index = 0

    async def receive():  # type: ignore[no-untyped-def]
        nonlocal sent_index
        if sent_index < len(chunks):
            body = chunks[sent_index]
            sent_index += 1
            return {"type": "http.request", "body": body, "more_body": sent_index < len(chunks)}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):  # type: ignore[no-untyped-def]
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/keys",
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
        "client": ("test", 1234),
    }

    asyncio.run(middleware(scope, receive, send))

    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 413


# --- R24: Deps.close() closes everything it owns ----------------------


class _Closeable:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _ProviderWithCache:
    def __init__(self, cache: _Closeable) -> None:
        self.cache = cache


def test_r24_deps_close_closes_provider_cache_and_others(tmp_path: Path) -> None:
    cache = _Closeable()
    provider = _ProviderWithCache(cache)
    renderer = MockAudioRenderer(out_dir=tmp_path / "artifacts")
    deps = Deps(
        provider=provider,  # type: ignore[arg-type]
        llm=MockLLMClient(),
        composer=MockScriptComposer(),
        renderer=renderer,
    )

    deps.close()

    assert cache.closed is True  # nothing raised for llm/composer/renderer/artifacts lacking close()


def test_r24_lifespan_closes_deps(tmp_path: Path) -> None:
    cache = _Closeable()
    provider = _ProviderWithCache(cache)
    deps = Deps(
        provider=provider,  # type: ignore[arg-type]
        llm=MockLLMClient(),
        composer=MockScriptComposer(),
        renderer=MockAudioRenderer(out_dir=tmp_path / "artifacts"),
    )
    store = SqliteJobStore(tmp_path / "jobs.db")
    app = create_app(store, deps)
    with TestClient(app):
        assert not cache.closed
    assert cache.closed is True
