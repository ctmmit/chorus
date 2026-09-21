from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from chorus.app import create_app
from chorus.audio import MockAudioRenderer
from chorus.email import MockEmailSender
from chorus.jobs import SqliteJobStore
from chorus.keys import SqliteKeyStore
from chorus.llm import MockLLMClient
from chorus.pipeline import Deps
from chorus.script import MockScriptComposer
from chorus.transcripts import FixtureTranscriptProvider


def _client(tmp_path: Path) -> tuple[TestClient, SqliteKeyStore, MockEmailSender, Path]:
    db_path = tmp_path / "chorus.db"
    store = SqliteJobStore(db_path)
    key_store = SqliteKeyStore(db_path)
    sender = MockEmailSender()
    deps = Deps(
        provider=FixtureTranscriptProvider(),
        llm=MockLLMClient(),
        composer=MockScriptComposer(),
        renderer=MockAudioRenderer(out_dir=tmp_path / "artifacts"),
    )
    app = create_app(
        store,
        deps,
        api_token="master-token",
        key_store=key_store,
        email_sender=sender,
    )
    return TestClient(app), key_store, sender, db_path


def _issued_token(sender: MockEmailSender) -> str:
    assert len(sender.sent) == 1
    return sender.sent[0].text.strip().splitlines()[-1]


def test_issue_email_token_authorize_then_revoke(tmp_path: Path) -> None:
    client, key_store, sender, _ = _client(tmp_path)

    response = client.post("/keys", json={"email": "principal@example.com"})

    assert response.status_code == 202
    assert not response.content
    token = _issued_token(sender)
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/shows", headers=headers).status_code == 200
    key_store.revoke(token)
    assert client.get("/shows", headers=headers).status_code == 401


def test_key_issue_rate_limit_returns_429(tmp_path: Path) -> None:
    client, _, _, _ = _client(tmp_path)
    payload = {"email": "principal@example.com"}

    assert client.post("/keys", json=payload).status_code == 202
    limited = client.post("/keys", json=payload)

    assert limited.status_code == 429
    assert "one API key" in limited.json()["detail"]


def test_master_token_still_works(tmp_path: Path) -> None:
    client, _, _, _ = _client(tmp_path)
    headers = {"Authorization": "Bearer master-token"}

    assert client.get("/shows", headers=headers).status_code == 200


def test_database_stores_hash_only(tmp_path: Path) -> None:
    client, _, sender, db_path = _client(tmp_path)
    assert client.post("/keys", json={"email": "principal@example.com"}).status_code == 202
    token = _issued_token(sender)

    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT key_hash, email FROM api_keys").fetchone()

    assert row == (hashlib.sha256(token.encode("utf-8")).hexdigest(), "principal@example.com")
    assert token.encode("utf-8") not in db_path.read_bytes()
