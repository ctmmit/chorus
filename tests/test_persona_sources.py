"""Personas as sources (chorus/publications.py): publishing, listening
through a PersonaSource, grounding across the hop, and endorsements."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from chorus.app import create_app
from chorus.artifacts import LocalArtifactStore
from chorus.audio import MockAudioRenderer
from chorus.curation import citation_resolves
from chorus.email import MockEmailSender
from chorus.feedback import FeedbackService, MockProposalWriter, SqliteFeedbackStore
from chorus.feeds import FeedFetchError, list_persona_episodes, list_recent_episodes
from chorus.jobs import MASTER_OWNER, SqliteJobStore
from chorus.llm import MockLLMClient
from chorus.models import DigestRequest, EpisodeInput, JobStatus
from chorus.personas import SqlitePersonaRegistry, build_persona
from chorus.pipeline import Deps, run_job
from chorus.publications import (
    PublishError,
    PublishService,
    SqlitePublicationStore,
    endorsement_hook,
    original_episode,
    persona_feed_episodes,
    publication_from,
)
from chorus.scheduler import run_subscription
from chorus.script import MockScriptComposer
from chorus.subscriptions import PersonaSource, SqliteSubscriptionStore, Subscription
from chorus.transcripts import FixtureTranscriptProvider

FIX = Path(__file__).resolve().parent.parent / "fixtures"
INVESTOR = (FIX / "souls" / "soul_investor.md").read_text(encoding="utf-8")
CONTEXT = (FIX / "context.md").read_text(encoding="utf-8")
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
TOKEN = "s3cret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHORUS_UNSUBSCRIBE_SECRET", "links-secret-for-tests")


def _deps(tmp_path: Path) -> Deps:
    return Deps(FixtureTranscriptProvider(), MockLLMClient(), MockScriptComposer(),
                MockAudioRenderer(out_dir=tmp_path), LocalArtifactStore(tmp_path))


def _digest_job(store: SqliteJobStore, deps: Deps, owner: str, *video_ids: str) -> str:
    job_id = store.create(owner=owner)
    request = DigestRequest(soul=INVESTOR, context=CONTEXT,
                            episodes=[EpisodeInput(video_id=v) for v in video_ids])
    run_job(job_id, request, store, deps)
    return job_id


class _World:
    def __init__(self, tmp_path: Path) -> None:
        db = tmp_path / "jobs.db"
        self.store = SqliteJobStore(db)
        self.deps = _deps(tmp_path)
        self.registry = SqlitePersonaRegistry(db)
        self.publications = SqlitePublicationStore(db)
        self.service = PublishService(self.publications, self.registry, self.store,
                                      clock=lambda: NOW)

    def lister(self, source: Any, since: datetime, limit: int, **_: Any) -> list[Any]:
        if isinstance(source, PersonaSource):
            return list_persona_episodes(source, since, limit, personas=self.registry,
                                         publications=self.publications)
        return list_recent_episodes(source, since, limit)


# --- pure logic --------------------------------------------------------------------


def test_publication_snapshots_original_episodes_with_highlights(tmp_path: Path) -> None:
    world = _World(tmp_path)
    job_id = _digest_job(world.store, world.deps, "alice", "sample_public", "IAgmW_gTxls")
    job = world.store.get(job_id)
    assert job is not None
    publication = publication_from(job, "p1", NOW)
    assert [p.episode.resolved_id() for p in publication.episodes] == ["sample_public"]
    assert publication.episodes[0].highlights  # the refused episode is left out

    unfinished = job.model_copy(update={"status": JobStatus.failed})
    with pytest.raises(PublishError, match="no finished digest"):
        publication_from(unfinished, "p1", NOW)


def test_original_episode_falls_back_for_older_digests() -> None:
    from chorus.models import EpisodeDigest

    youtube = EpisodeDigest(episode_id="abc", episode_title=None, highlights=[])
    rss = EpisodeDigest(episode_id="rss-1", episode_title=None, highlights=[],
                        url="https://x.example/a.mp3")
    lost = EpisodeDigest(episode_id="rss-2", episode_title=None, highlights=[])
    assert original_episode(youtube) == EpisodeInput(video_id="abc")
    assert original_episode(rss) == EpisodeInput(audio_url="https://x.example/a.mp3")
    assert original_episode(lost) is None


def test_persona_feed_episodes_are_recent_unique_and_tagged(tmp_path: Path) -> None:
    world = _World(tmp_path)
    persona = world.registry.create(build_persona(name="Margins", soul=INVESTOR, owner="alice"))
    job = world.store.get(_digest_job(world.store, world.deps, "alice", "sample_public"))
    assert job is not None
    old = publication_from(job, persona.persona_id, NOW - timedelta(days=30))
    new = publication_from(job, persona.persona_id, NOW)
    episodes = persona_feed_episodes(persona, [old, new], NOW - timedelta(days=7), 10)
    assert [e.episode.resolved_id() for e in episodes] == ["sample_public"]
    assert episodes[0].via_persona == persona.persona_id
    assert episodes[0].source_title == "Margins (persona)"


def test_private_or_unknown_personas_cannot_be_listened_to(tmp_path: Path) -> None:
    world = _World(tmp_path)
    private = world.registry.create(build_persona(name="Mine", soul=INVESTOR, public=False))
    for persona_id in (private.persona_id, "nope"):
        with pytest.raises(FeedFetchError, match="unknown or not public"):
            list_persona_episodes(PersonaSource(kind="persona", persona_id=persona_id), NOW, 5,
                                  personas=world.registry, publications=world.publications)


def test_only_the_owner_may_publish(tmp_path: Path) -> None:
    world = _World(tmp_path)
    persona = world.registry.create(build_persona(name="A", soul=INVESTOR, owner="alice"))
    job_id = _digest_job(world.store, world.deps, "alice", "sample_public")
    with pytest.raises(PublishError, match="unknown persona"):
        world.service.publish("mallory", persona.persona_id, job_id)
    mallory_job = _digest_job(world.store, world.deps, "mallory", "sample_public")
    with pytest.raises(PublishError, match="unknown job"):
        world.service.publish("alice", persona.persona_id, mallory_job)
    assert world.service.publish("alice", persona.persona_id, job_id).job_id == job_id


# --- the plan's integration check -------------------------------------------------------


def test_b_hears_a_through_its_own_soul_and_endorses_it(tmp_path: Path) -> None:
    """A runs and publishes; B subscribes to A as a source and runs; B's
    highlights cite A's original source episode and timestamp; B's up-vote
    on one increments A's endorsement count."""
    world = _World(tmp_path)
    a = world.registry.create(build_persona(name="Margins", soul=INVESTOR, owner="alice"))
    a_job_id = _digest_job(world.store, world.deps, "alice", "sample_public")
    world.service.publish("alice", a.persona_id, a_job_id)
    a_job = world.store.get(a_job_id)
    assert a_job is not None and a_job.digest is not None
    originals = {(h.episode_id, h.segment_timestamp) for h in a_job.digest.highlights}

    subscriptions = SqliteSubscriptionStore(tmp_path / "jobs.db")
    sub = Subscription(
        subscription_id="b-sub", owner="bob", email="bob@example.com", soul=INVESTOR,
        context="", sources=[PersonaSource(kind="persona", persona_id=a.persona_id)],
        next_run_at=NOW, created_at=NOW - timedelta(days=1), lookback_days_first_run=7,
    )
    subscriptions.create(sub)
    b_job_id = run_subscription(sub, world.store, world.deps, subscriptions, MockEmailSender(),
                                "https://chorus.example", NOW, lister=world.lister)
    assert b_job_id is not None
    b_job = world.store.get(b_job_id)
    assert b_job is not None and b_job.status is JobStatus.done and b_job.digest is not None
    assert b_job.owner == "bob"

    [episode] = b_job.digest.episodes
    assert episode.episode_id == "sample_public" and episode.via_persona == a.persona_id
    assert episode.highlights
    # B curated the original episode through its own soul: every highlight
    # cites the primary source and resolves against its real transcript,
    # whether or not A surfaced the same moment.
    transcript = FixtureTranscriptProvider().get(EpisodeInput(video_id="sample_public"))
    for h in episode.highlights:
        assert h.episode_id == "sample_public"  # the primary source, not the persona
        assert citation_resolves(transcript, h.segment_timestamp, h.quote)
    assert {e for e, _ in originals} == {"sample_public"}

    feedback = FeedbackService(
        SqliteFeedbackStore(tmp_path / "jobs.db"), world.store, subscriptions,
        writer_factory=MockProposalWriter, on_rating=endorsement_hook(world.publications),
    )
    target = episode.highlights[0].highlight_id
    feedback.rate("bob", b_job_id, target, "up")
    feedback.rate("bob", b_job_id, target, "up")  # once per endorser and highlight
    assert world.publications.endorsement_counts() == {a.persona_id: 1}
    feedback.rate("bob", b_job_id, episode.highlights[-1].highlight_id, "down")
    assert world.publications.endorsement_counts() == {a.persona_id: 1}


# --- HTTP --------------------------------------------------------------------------------


def test_publish_route_public_listing_feed_and_network(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    client = TestClient(create_app(store, _deps(tmp_path), api_token=TOKEN))
    persona = client.post("/personas", json={"name": "Margins", "soul": INVESTOR},
                          headers=AUTH).json()
    assert persona["owner"] == MASTER_OWNER
    job_id = _digest_job(store, _deps(tmp_path), MASTER_OWNER, "sample_public")

    path = f"/personas/{persona['persona_id']}"
    assert client.post(f"{path}/publish", json={"job_id": job_id}).status_code == 401
    published = client.post(f"{path}/publish", json={"job_id": job_id}, headers=AUTH)
    assert published.status_code == 200
    assert client.post(f"{path}/publish", json={"job_id": "nope"}, headers=AUTH).status_code == 404

    listing = client.get(f"{path}/published")  # public, no token
    assert listing.status_code == 200 and listing.json()[0]["job_id"] == job_id
    feed = client.get(f"{path}/feed.xml")
    assert feed.status_code == 200 and "<rss" in feed.text  # mock audio is text: no items
    assert client.get(f"{path}/episodes/{job_id}.mp3").status_code == 404

    card = client.get(f"{path}/agent.json").json()
    assert any(s["id"] == "podcast" for s in card["skills"])
    nodes = {n["id"]: n for n in client.get("/network").json()["nodes"]}
    assert nodes[persona["persona_id"]]["endorsements"] == 0
