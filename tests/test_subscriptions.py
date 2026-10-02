"""Phase F (docs/DEVELOPMENT_PLAN.md §3, §8 row F): subscription store
contract, schedule arithmetic, the CRUD + unsubscribe + cron-tick API, and
the Monday-created/Friday-delivered end-to-end scenario with a mocked email
sender and a fake clock.

Uses the synthetic `sample_public` transcript (see tests/test_inngest_app.py)
so every test here runs without the private fixtures repo.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from chorus import subscriptions as subscriptions_module
from chorus.app import create_app
from chorus.artifacts import LocalArtifactStore
from chorus.audio import MockAudioRenderer
from chorus.email import MockEmailSender
from chorus.inngest_app import run_tick_body
from chorus.jobs import SqliteJobStore
from chorus.keys import SqliteKeyStore
from chorus.llm import MockLLMClient
from chorus.models import EpisodeInput, JobStatus
from chorus.pipeline import Deps
from chorus.scheduler import due_subscriptions, next_run, run_subscription
from chorus.script import MockScriptComposer
from chorus.subscriptions import (
    MASTER_OWNER,
    SqliteSubscriptionStore,
    Subscription,
    SubscriptionStore,
    unsubscribe_token,
    verify_unsubscribe_token,
)
from chorus.transcripts import FixtureTranscriptProvider

FIX = Path(__file__).resolve().parent.parent / "fixtures"
SAMPLE = "sample_public"
SOUL = (FIX / "souls" / "soul_investor.md").read_text(encoding="utf-8")

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(autouse=True)
def _unsubscribe_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    # Deterministic across a test, independent of the fallback-to-random-secret
    # path (chorus.subscriptions._unsubscribe_secret) and of CHORUS_API_TOKEN.
    monkeypatch.setenv("CHORUS_UNSUBSCRIBE_SECRET", "test-unsubscribe-secret")


def _deps(tmp_path: Path) -> Deps:
    return Deps(
        provider=FixtureTranscriptProvider(),
        llm=MockLLMClient(),
        composer=MockScriptComposer(),
        renderer=MockAudioRenderer(out_dir=tmp_path / "artifacts"),
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
    )


def _subscription(**overrides: object) -> Subscription:
    now = datetime.now(UTC)
    fields: dict[str, object] = {
        "subscription_id": uuid.uuid4().hex,
        "owner": MASTER_OWNER,
        "email": "principal@example.com",
        "soul": SOUL,
        "context": "",
        "episodes": [EpisodeInput(video_id=SAMPLE)],
        "highlight_count": 4,
        "cadence": "weekly",
        "next_run_at": now,
        "active": True,
        "created_at": now,
    }
    fields.update(overrides)
    return Subscription.model_validate(fields)


# --- Field-description discipline (mirrors tests/test_models.py) ----------


def test_every_subscriptions_pydantic_field_has_a_description() -> None:
    model_types = [
        value
        for value in vars(subscriptions_module).values()
        if inspect.isclass(value) and issubclass(value, BaseModel) and value is not BaseModel
    ]
    missing = [
        f"{model_type.__name__}.{name}"
        for model_type in model_types
        for name, field in model_type.model_fields.items()
        if not field.description
    ]
    assert missing == []


# --- SubscriptionStore contract (mirrors tests/test_store_contract.py) ----


def _sqlite_subscription_store(tmp_path: Path) -> SubscriptionStore:
    return SqliteSubscriptionStore(tmp_path / f"subs-{uuid.uuid4().hex}.db")


def _postgres_subscription_store(tmp_path: Path) -> SubscriptionStore:
    from chorus.stores.postgres import PostgresSubscriptionStore

    assert TEST_DATABASE_URL is not None
    return PostgresSubscriptionStore(TEST_DATABASE_URL)


SUBSCRIPTION_STORE_FACTORIES = [("sqlite", _sqlite_subscription_store)]
if TEST_DATABASE_URL:
    SUBSCRIPTION_STORE_FACTORIES.append(("postgres", _postgres_subscription_store))


@pytest.fixture(
    params=[f for _, f in SUBSCRIPTION_STORE_FACTORIES],
    ids=[n for n, _ in SUBSCRIPTION_STORE_FACTORIES],
)
def subscription_store(request: pytest.FixtureRequest, tmp_path: Path):  # type: ignore[no-untyped-def]
    store = request.param(tmp_path)
    yield store
    store.close()


def test_create_get_round_trips(subscription_store: SubscriptionStore) -> None:
    sub = _subscription()
    subscription_store.create(sub)
    fetched = subscription_store.get(sub.subscription_id)
    assert fetched == sub


def test_get_unknown_returns_none(subscription_store: SubscriptionStore) -> None:
    assert subscription_store.get(f"missing-{uuid.uuid4().hex}") is None


def test_list_all_and_scoped_by_owner(subscription_store: SubscriptionStore) -> None:
    a = _subscription(owner="a@example.com")
    b = _subscription(owner="b@example.com")
    subscription_store.create(a)
    subscription_store.create(b)

    assert {s.subscription_id for s in subscription_store.list(owner="a@example.com")} == {
        a.subscription_id
    }
    all_ids = {s.subscription_id for s in subscription_store.list()}
    assert {a.subscription_id, b.subscription_id} <= all_ids


def test_save_updates_existing_row(subscription_store: SubscriptionStore) -> None:
    sub = _subscription()
    subscription_store.create(sub)
    sub.active = False
    sub.context = "refreshed"
    subscription_store.save(sub)

    reloaded = subscription_store.get(sub.subscription_id)
    assert reloaded is not None
    assert reloaded.active is False
    assert reloaded.context == "refreshed"


def test_delete_removes_row(subscription_store: SubscriptionStore) -> None:
    sub = _subscription()
    subscription_store.create(sub)
    subscription_store.delete(sub.subscription_id)
    assert subscription_store.get(sub.subscription_id) is None


def test_due_returns_only_active_and_past_due(subscription_store: SubscriptionStore) -> None:
    now = datetime.now(UTC)
    due_sub = _subscription(next_run_at=now - timedelta(hours=1))
    future_sub = _subscription(next_run_at=now + timedelta(hours=1))
    inactive_sub = _subscription(next_run_at=now - timedelta(hours=1), active=False)
    for s in (due_sub, future_sub, inactive_sub):
        subscription_store.create(s)

    due_ids = {s.subscription_id for s in subscription_store.due(now)}
    assert due_ids == {due_sub.subscription_id}


def test_due_subscriptions_wraps_store_due(subscription_store: SubscriptionStore) -> None:
    now = datetime.now(UTC)
    due_sub = _subscription(next_run_at=now - timedelta(minutes=1))
    subscription_store.create(due_sub)
    assert [s.subscription_id for s in due_subscriptions(subscription_store, now)] == [
        due_sub.subscription_id
    ]


# --- next_run arithmetic ---------------------------------------------------

# 2026-09-21 is a Monday (UTC), 2026-09-25 the following Friday, verified
# against datetime.weekday() rather than assumed.
MONDAY = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)
FRIDAY_BEFORE_HOUR = datetime(2026, 9, 25, 8, 0, tzinfo=UTC)
FRIDAY_AT_RUN_HOUR = datetime(2026, 9, 25, 13, 0, tzinfo=UTC)
NEXT_FRIDAY_AT_RUN_HOUR = datetime(2026, 10, 2, 13, 0, tzinfo=UTC)


def test_next_run_weekly_from_monday_lands_on_friday_same_week() -> None:
    assert next_run("weekly", MONDAY) == FRIDAY_AT_RUN_HOUR


def test_next_run_weekly_friday_before_run_hour_same_day() -> None:
    assert next_run("weekly", FRIDAY_BEFORE_HOUR) == FRIDAY_AT_RUN_HOUR


def test_next_run_weekly_at_exact_run_moment_rolls_to_next_week() -> None:
    # Strictly after `after`: a subscription run exactly at its scheduled
    # moment must not schedule itself again immediately.
    assert next_run("weekly", FRIDAY_AT_RUN_HOUR) == NEXT_FRIDAY_AT_RUN_HOUR


def test_next_run_daily_rolls_to_tomorrow_when_past_hour() -> None:
    after = datetime(2026, 9, 21, 14, 0, tzinfo=UTC)
    assert next_run("daily", after) == datetime(2026, 9, 22, 13, 0, tzinfo=UTC)


def test_next_run_daily_same_day_when_before_hour() -> None:
    after = datetime(2026, 9, 21, 8, 0, tzinfo=UTC)
    assert next_run("daily", after) == datetime(2026, 9, 21, 13, 0, tzinfo=UTC)


def test_next_run_requires_tz_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        next_run("weekly", datetime(2026, 9, 21, 10, 0))


def test_next_run_unknown_cadence_raises() -> None:
    with pytest.raises(ValueError, match="cadence"):
        next_run("monthly", MONDAY)


# --- API: CRUD with owner scoping ------------------------------------------


def _client(tmp_path: Path, api_token: str = "master-token"):  # type: ignore[no-untyped-def]
    db_path = tmp_path / "chorus.db"
    store = SqliteJobStore(db_path)
    key_store = SqliteKeyStore(db_path)
    subscription_store = SqliteSubscriptionStore(db_path)
    sender = MockEmailSender()
    deps = _deps(tmp_path)
    app = create_app(
        store,
        deps,
        api_token=api_token,
        key_store=key_store,
        email_sender=sender,
        subscription_store=subscription_store,
    )
    return TestClient(app), key_store, subscription_store, sender


def _payload(**overrides: object) -> dict:
    body = {
        "email": "principal@example.com",
        "soul": SOUL,
        "context": "",
        "episodes": [{"video_id": SAMPLE}],
        "highlight_count": 4,
        "cadence": "weekly",
    }
    body.update(overrides)
    return body


MASTER_HEADERS = {"Authorization": "Bearer master-token"}


def test_create_requires_episodes_or_shows(tmp_path: Path) -> None:
    client, *_ = _client(tmp_path)
    body = _payload()
    del body["episodes"]
    r = client.post("/subscriptions", json=body, headers=MASTER_HEADERS)
    assert r.status_code == 422


def test_create_and_get_subscription_as_master(tmp_path: Path) -> None:
    client, *_ = _client(tmp_path)
    r = client.post("/subscriptions", json=_payload(), headers=MASTER_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["owner"] == "master"
    assert body["active"] is True

    got = client.get(f"/subscriptions/{body['subscription_id']}", headers=MASTER_HEADERS)
    assert got.status_code == 200
    assert got.json()["subscription_id"] == body["subscription_id"]


def test_owner_scoping_cross_owner_404_not_403(tmp_path: Path) -> None:
    client, key_store, *_ = _client(tmp_path)
    token_a = key_store.issue("a@example.com")
    token_b = key_store.issue("b@example.com")
    h_a = {"Authorization": f"Bearer {token_a}"}
    h_b = {"Authorization": f"Bearer {token_b}"}

    created = client.post("/subscriptions", json=_payload(), headers=h_a).json()
    sub_id = created["subscription_id"]
    assert created["owner"] == "a@example.com"

    # A different key cannot see it — 404, not 403, to avoid enumeration.
    assert client.get(f"/subscriptions/{sub_id}", headers=h_b).status_code == 404
    assert client.patch(f"/subscriptions/{sub_id}", json={"active": False}, headers=h_b).status_code == 404
    assert client.delete(f"/subscriptions/{sub_id}", headers=h_b).status_code == 404

    # Its own owner and the master token both can.
    assert client.get(f"/subscriptions/{sub_id}", headers=h_a).status_code == 200
    assert client.get(f"/subscriptions/{sub_id}", headers=MASTER_HEADERS).status_code == 200

    listed_a = {s["subscription_id"] for s in client.get("/subscriptions", headers=h_a).json()}
    assert listed_a == {sub_id}
    listed_master = {s["subscription_id"] for s in client.get("/subscriptions", headers=MASTER_HEADERS).json()}
    assert sub_id in listed_master


def test_patch_context_refresh_and_active_toggle(tmp_path: Path) -> None:
    client, *_ = _client(tmp_path)
    sub_id = client.post("/subscriptions", json=_payload(), headers=MASTER_HEADERS).json()["subscription_id"]

    r = client.patch(
        f"/subscriptions/{sub_id}",
        json={"context": "fresh context this week", "active": False, "cadence": "daily"},
        headers=MASTER_HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["context"] == "fresh context this week"
    assert body["active"] is False
    assert body["cadence"] == "daily"


def test_patch_unknown_subscription_is_404(tmp_path: Path) -> None:
    client, *_ = _client(tmp_path)
    r = client.patch("/subscriptions/does-not-exist", json={"active": False}, headers=MASTER_HEADERS)
    assert r.status_code == 404


def test_delete_subscription(tmp_path: Path) -> None:
    client, *_ = _client(tmp_path)
    sub_id = client.post("/subscriptions", json=_payload(), headers=MASTER_HEADERS).json()["subscription_id"]

    assert client.delete(f"/subscriptions/{sub_id}", headers=MASTER_HEADERS).status_code == 204
    assert client.get(f"/subscriptions/{sub_id}", headers=MASTER_HEADERS).status_code == 404


# --- run-now ----------------------------------------------------------------


def test_run_now_returns_a_job_that_reaches_done(tmp_path: Path) -> None:
    client, *_ = _client(tmp_path)
    sub_id = client.post("/subscriptions", json=_payload(), headers=MASTER_HEADERS).json()["subscription_id"]

    r = client.post(f"/subscriptions/{sub_id}/run", headers=MASTER_HEADERS)
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    job = client.get(f"/digest/{job_id}", headers=MASTER_HEADERS).json()
    assert job["status"] == "done"

    # run-now also records last_job_id/last_run_at and advances the schedule.
    sub = client.get(f"/subscriptions/{sub_id}", headers=MASTER_HEADERS).json()
    assert sub["last_job_id"] == job_id
    assert sub["last_run_at"] is not None


# --- unsubscribe -------------------------------------------------------------


def test_unsubscribe_bad_signature_is_403(tmp_path: Path) -> None:
    client, *_ = _client(tmp_path)
    sub_id = client.post("/subscriptions", json=_payload(), headers=MASTER_HEADERS).json()["subscription_id"]

    r = client.get(f"/subscriptions/{sub_id}/unsubscribe", params={"token": "not-the-right-signature"})
    assert r.status_code == 403


def test_unsubscribe_good_signature_deactivates_and_is_idempotent(tmp_path: Path) -> None:
    client, *_ = _client(tmp_path)
    sub_id = client.post("/subscriptions", json=_payload(), headers=MASTER_HEADERS).json()["subscription_id"]
    token = unsubscribe_token(sub_id)

    r1 = client.get(f"/subscriptions/{sub_id}/unsubscribe", params={"token": token})
    assert r1.status_code == 200
    assert client.get(f"/subscriptions/{sub_id}", headers=MASTER_HEADERS).json()["active"] is False

    # A second click with the same valid link is a no-op, not an error.
    r2 = client.get(f"/subscriptions/{sub_id}/unsubscribe", params={"token": token})
    assert r2.status_code == 200


def test_unsubscribe_requires_no_bearer_token(tmp_path: Path) -> None:
    # Public route: no Authorization header sent at all, and the service
    # still has a token configured ("master-token") so this proves the
    # exemption, not merely that auth is globally off.
    client, *_ = _client(tmp_path)
    sub_id = client.post("/subscriptions", json=_payload(), headers=MASTER_HEADERS).json()["subscription_id"]
    token = unsubscribe_token(sub_id)
    r = client.get(f"/subscriptions/{sub_id}/unsubscribe", params={"token": token})
    assert r.status_code == 200


# --- cron tick ---------------------------------------------------------------


def test_cron_tick_without_configured_secret_is_503(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CRON_SECRET", raising=False)
    client, *_ = _client(tmp_path)
    assert client.post("/internal/cron/tick").status_code == 503


def test_cron_tick_requires_correct_bearer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRON_SECRET", "tick-secret")
    client, *_ = _client(tmp_path)

    assert client.post("/internal/cron/tick").status_code == 401
    assert client.post(
        "/internal/cron/tick", headers={"Authorization": "Bearer wrong"}
    ).status_code == 401
    r = client.post("/internal/cron/tick", headers={"Authorization": "Bearer tick-secret"})
    assert r.status_code == 200
    assert r.json()["ran"] == 0


def test_cron_tick_also_accepts_get_like_vercel_cron(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Vercel Cron always issues a GET to the configured path — the route
    # must accept that, not just POST.
    monkeypatch.setenv("CRON_SECRET", "tick-secret")
    client, *_ = _client(tmp_path)
    r = client.get("/internal/cron/tick", headers={"Authorization": "Bearer tick-secret"})
    assert r.status_code == 200


# --- Monday -> Friday end-to-end (exit criterion) ----------------------------


def test_monday_subscription_delivers_friday_email_with_working_deep_links(tmp_path: Path) -> None:
    db_path = tmp_path / "chorus.db"
    store = SqliteJobStore(db_path)
    subscription_store = SqliteSubscriptionStore(db_path)
    sender = MockEmailSender()
    deps = _deps(tmp_path)

    # Created "Monday" -> scheduled for "Friday" at the weekly run hour.
    scheduled_for = next_run("weekly", MONDAY)
    assert scheduled_for == FRIDAY_AT_RUN_HOUR
    assert scheduled_for.weekday() == 4  # Friday

    subscription = _subscription(
        episodes=[EpisodeInput(video_id=SAMPLE)],
        cadence="weekly",
        next_run_at=scheduled_for,
        created_at=MONDAY,
    )
    subscription_store.create(subscription)

    # Fake clock: "now" is exactly the scheduled Friday moment.
    friday_now = scheduled_for
    due = due_subscriptions(subscription_store, friday_now)
    assert [s.subscription_id for s in due] == [subscription.subscription_id]

    base_url = "https://chorus.example.com"
    job_id = run_subscription(due[0], store, deps, subscription_store, sender, base_url, friday_now)

    job = store.get(job_id)
    assert job is not None and job.status == JobStatus.done
    assert job.digest is not None and job.digest.episodes

    assert len(sender.sent) == 1
    email = sender.sent[0]
    assert email.to == subscription.email

    # Every highlight's deep link is in the email and matches the job.
    highlights = [h for ep in job.digest.episodes for h in ep.highlights]
    assert highlights, "fixture soul should surface at least one highlight"
    for h in highlights:
        expected_link = f"https://www.youtube.com/watch?v={h.episode_id}&t={int(h.segment_timestamp)}s"
        assert expected_link in email.text

    # List-Unsubscribe headers present (Gmail/Yahoo bulk-sender requirement).
    assert email.headers is not None
    assert "List-Unsubscribe" in email.headers
    assert email.headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"

    # The unsubscribe URL in the email verifies against the signing secret.
    match = re.search(r"Unsubscribe: (\S+)", email.text)
    assert match is not None
    unsubscribe_url = match.group(1)
    assert unsubscribe_url.startswith(base_url)
    token = unsubscribe_url.rsplit("token=", 1)[-1]
    assert verify_unsubscribe_token(subscription.subscription_id, token)

    # The subscription advanced past this run.
    reloaded = subscription_store.get(subscription.subscription_id)
    assert reloaded is not None
    assert reloaded.last_job_id == job_id
    assert reloaded.last_run_at == friday_now
    assert reloaded.next_run_at == NEXT_FRIDAY_AT_RUN_HOUR


def test_failed_job_still_advances_schedule_and_sends_failure_email(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    subscription_store = SqliteSubscriptionStore(tmp_path / "jobs.db")
    sender = MockEmailSender()
    deps = _deps(tmp_path)

    now = datetime.now(UTC)
    # An episode id the fixture provider cannot resolve -> all-transcripts-
    # failed -> job ends `failed`, never stuck.
    subscription = _subscription(
        episodes=[EpisodeInput(video_id="does-not-exist")], next_run_at=now
    )
    subscription_store.create(subscription)

    job_id = run_subscription(
        subscription, store, deps, subscription_store, sender, "https://chorus.example.com", now
    )

    job = store.get(job_id)
    assert job is not None and job.status == JobStatus.failed

    assert len(sender.sent) == 1
    assert "failed" in sender.sent[0].subject.lower()
    assert job.error in sender.sent[0].text

    reloaded = subscription_store.get(subscription.subscription_id)
    assert reloaded is not None
    assert reloaded.last_job_id == job_id
    assert reloaded.next_run_at > now  # never left stuck at the same next_run_at


# --- Inngest chorus/tick function (fake step, mirrors test_inngest_app.py) --


class _FakeStep:
    async def run(self, step_id: str, handler):  # type: ignore[no-untyped-def]
        return await handler()


def test_run_tick_body_runs_every_due_subscription(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    subscription_store = SqliteSubscriptionStore(tmp_path / "jobs.db")
    sender = MockEmailSender()
    deps = _deps(tmp_path)

    past = datetime.now(UTC) - timedelta(hours=1)
    due_sub = _subscription(next_run_at=past)
    future_sub = _subscription(next_run_at=datetime.now(UTC) + timedelta(days=1))
    subscription_store.create(due_sub)
    subscription_store.create(future_sub)

    result = asyncio.run(run_tick_body(_FakeStep(), store, deps, subscription_store, sender))

    assert result["ran"] == 1
    assert len(sender.sent) == 1

    reloaded_due = subscription_store.get(due_sub.subscription_id)
    assert reloaded_due is not None
    assert reloaded_due.next_run_at > past

    reloaded_future = subscription_store.get(future_sub.subscription_id)
    assert reloaded_future is not None
    assert reloaded_future.last_job_id is None  # untouched: not due
