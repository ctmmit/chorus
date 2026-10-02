"""Goal 3.3 — async lifecycle end to end via the API, offline (fixtures + mocks).

TestClient runs background tasks before returning, so the job is terminal by the
time we poll.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chorus.app import create_app
from chorus.artifacts import LocalArtifactStore
from chorus.audio import MockAudioRenderer
from chorus.jobs import SqliteJobStore
from chorus.llm import MockLLMClient
from chorus.models import TWO_HOST_PROFILE
from chorus.pipeline import Deps
from chorus.script import MockScriptComposer
from chorus.transcripts import FixtureTranscriptProvider

FIX = Path(__file__).resolve().parent.parent / "fixtures"
ANDREESSEN = "c4tvVKDhpiY"
GOLDMAN_MISSING = "KhZfxZ-C-2g"


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    deps = Deps(
        provider=FixtureTranscriptProvider(),
        llm=MockLLMClient(),
        composer=MockScriptComposer(),
        renderer=MockAudioRenderer(out_dir=tmp_path / "artifacts"),
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
    )
    return TestClient(create_app(SqliteJobStore(tmp_path / "jobs.db"), deps))


def _payload(*video_ids: str) -> dict:
    return {
        "soul": (FIX / "souls" / "soul_investor.md").read_text(encoding="utf-8"),
        "context": (FIX / "context.md").read_text(encoding="utf-8"),
        "episodes": [{"video_id": v} for v in video_ids],
        "highlight_count": 4,
    }


def test_post_returns_job_id(client: TestClient) -> None:
    r = client.post("/digest", json=_payload(ANDREESSEN))
    assert r.status_code == 200
    assert r.json()["job_id"]


def test_full_lifecycle_reaches_done(client: TestClient) -> None:
    job_id = client.post("/digest", json=_payload(ANDREESSEN)).json()["job_id"]
    body = client.get(f"/digest/{job_id}").json()
    assert body["status"] == "done"
    assert body["digest"]["episodes"][0]["highlights"]
    assert body["audio_url"]


def test_two_host_profile_lifecycle_reaches_done_with_dialogue_script(client: TestClient) -> None:
    # §8 row E exit criterion: two-host episode renders; every turn resolves
    # to a highlight; single-voice (the fixture above) still passes.
    payload = _payload(ANDREESSEN)
    payload["profile"] = TWO_HOST_PROFILE.model_dump()
    job_id = client.post("/digest", json=payload).json()["job_id"]
    body = client.get(f"/digest/{job_id}").json()

    assert body["status"] == "done"
    assert body["script"]["format"] == "dialogue"
    assert body["script"]["turns"], "two-host profile should yield dialogue turns"
    valid = {
        (h["episode_id"], round(h["segment_timestamp"]))
        for ep in body["digest"]["episodes"]
        for h in ep["highlights"]
    }
    for turn in body["script"]["turns"]:
        assert (turn["episode_id"], round(turn["segment_timestamp"])) in valid
    assert body["audio_url"]


def test_all_transcripts_fail_returns_failed(client: TestClient) -> None:
    # The flagged critical gap: must be failed with a reason, never empty done.
    job_id = client.post("/digest", json=_payload(GOLDMAN_MISSING)).json()["job_id"]
    body = client.get(f"/digest/{job_id}").json()
    assert body["status"] == "failed"
    assert body["error"]


def test_unknown_job_id_404(client: TestClient) -> None:
    assert client.get("/digest/does-not-exist").status_code == 404


# --- Must-fix regressions (codebase review, 21 Sep 2026) -------------------


class _RaisingLLM:
    def score_segment(self, text: str, soul: str, context: str) -> tuple[float, str]:
        raise RuntimeError("simulated provider outage")

    def score_windows(
        self, windows: list[str], soul: str, context: str, meter: object | None = None
    ) -> list[tuple[float, str]]:
        raise RuntimeError("simulated provider outage")


class _RaisingComposer:
    def write_script(self, digest, soul, context):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated composer outage")


class _RaisingRenderer:
    def render(self, script, soul, job_id):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated tts outage")


def _client_with(tmp_path: Path, **overrides: object) -> TestClient:
    kw: dict = {
        "provider": FixtureTranscriptProvider(),
        "llm": MockLLMClient(),
        "composer": MockScriptComposer(),
        "renderer": MockAudioRenderer(out_dir=tmp_path / "artifacts"),
        "artifacts": LocalArtifactStore(tmp_path / "artifacts"),
    }
    kw.update(overrides)
    return TestClient(create_app(SqliteJobStore(tmp_path / "jobs.db"), Deps(**kw)))


def test_llm_exception_ends_failed_not_stuck(tmp_path: Path) -> None:
    c = _client_with(tmp_path, llm=_RaisingLLM())
    job_id = c.post("/digest", json=_payload(ANDREESSEN)).json()["job_id"]
    body = c.get(f"/digest/{job_id}").json()
    assert body["status"] == "failed"
    assert "simulated provider outage" in body["error"]


def test_composer_exception_degrades_to_done_with_warning(tmp_path: Path) -> None:
    c = _client_with(tmp_path, composer=_RaisingComposer())
    job_id = c.post("/digest", json=_payload(ANDREESSEN)).json()["job_id"]
    body = c.get(f"/digest/{job_id}").json()
    assert body["status"] == "done"
    assert body["digest"]["episodes"][0]["highlights"]  # digest still delivered
    assert body["script"] is None and body["audio_url"] is None
    assert any("script synthesis failed" in w for w in body["warnings"])


def test_renderer_exception_is_recorded_as_warning(tmp_path: Path) -> None:
    c = _client_with(tmp_path, renderer=_RaisingRenderer())
    job_id = c.post("/digest", json=_payload(ANDREESSEN)).json()["job_id"]
    body = c.get(f"/digest/{job_id}").json()
    assert body["status"] == "done"
    assert body["script"] is not None
    assert body["audio_url"] is None
    assert any("audio render failed" in w for w in body["warnings"])


def test_audio_urls_are_unique_per_job(client: TestClient) -> None:
    j1 = client.post("/digest", json=_payload(ANDREESSEN)).json()["job_id"]
    j2 = client.post("/digest", json=_payload("xKZ_8ULR91Y")).json()["job_id"]
    a1 = client.get(f"/digest/{j1}").json()["audio_url"]
    a2 = client.get(f"/digest/{j2}").json()["audio_url"]
    assert a1 and a2 and a1 != a2
    assert j1 in a1 and j2 in a2


def test_startup_sweeps_in_flight_jobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    store = SqliteJobStore(tmp_path / "jobs.db")
    stranded = store.create()  # queued, and no background task will ever run it
    deps = Deps(FixtureTranscriptProvider(), MockLLMClient(), MockScriptComposer(),
                MockAudioRenderer(out_dir=tmp_path / "artifacts"),
                LocalArtifactStore(tmp_path / "artifacts"))
    with TestClient(create_app(store, deps)) as c:  # `with` runs the lifespan
        body = c.get(f"/digest/{stranded}").json()
    assert body["status"] == "failed"
    assert "restart" in body["error"]


def test_startup_refuses_real_keys_without_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("CHORUS_API_TOKEN", raising=False)
    deps = Deps(FixtureTranscriptProvider(), MockLLMClient(), MockScriptComposer(),
                MockAudioRenderer(out_dir=tmp_path / "artifacts"),
                LocalArtifactStore(tmp_path / "artifacts"))
    with (
        pytest.raises(RuntimeError, match="CHORUS_API_TOKEN"),
        TestClient(create_app(SqliteJobStore(tmp_path / "jobs.db"), deps)),
    ):
        pass


@pytest.fixture
def secured(tmp_path: Path) -> TestClient:
    deps = Deps(FixtureTranscriptProvider(), MockLLMClient(), MockScriptComposer(),
                MockAudioRenderer(out_dir=tmp_path / "artifacts"),
                LocalArtifactStore(tmp_path / "artifacts"))
    return TestClient(create_app(SqliteJobStore(tmp_path / "jobs.db"), deps, api_token="s3cret"))


def test_bearer_token_required_on_every_route(secured: TestClient) -> None:
    assert secured.get("/shows").status_code == 401
    assert secured.post("/digest", json=_payload(ANDREESSEN)).status_code == 401
    assert secured.get("/artifacts/anything.mp3").status_code == 401
    assert secured.get("/shows", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert secured.get("/shows", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_bearer_token_allows_full_lifecycle(secured: TestClient) -> None:
    h = {"Authorization": "Bearer s3cret"}
    job_id = secured.post("/digest", json=_payload(ANDREESSEN), headers=h).json()["job_id"]
    assert secured.get(f"/digest/{job_id}", headers=h).json()["status"] == "done"


@pytest.mark.parametrize("count", [0, -1, 21])
def test_highlight_count_out_of_range_is_422(client: TestClient, count: int) -> None:
    assert client.post("/digest", json={**_payload(ANDREESSEN), "highlight_count": count}).status_code == 422


def test_oversized_fields_are_422(client: TestClient) -> None:
    assert client.post("/digest", json={**_payload(ANDREESSEN), "soul": "x" * 40_001}).status_code == 422
    assert client.post("/digest", json={**_payload(ANDREESSEN), "context": "x" * 40_001}).status_code == 422
    too_many = [{"video_id": ANDREESSEN}] * 26
    assert client.post("/digest", json={**_payload(ANDREESSEN), "episodes": too_many}).status_code == 422
    assert client.post("/digest", json={**_payload(ANDREESSEN), "episodes": []}).status_code == 422


def test_oversized_body_is_413_before_parsing(client: TestClient) -> None:
    r = client.post("/digest", json={**_payload(ANDREESSEN), "soul": "x" * 2_000_000})
    assert r.status_code == 413
