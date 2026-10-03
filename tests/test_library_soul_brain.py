"""`soul_from_library` honors the onboarding brain on a local install.

With `brain=host` Chorus calls no model, so the library soul is a template
draft plus the library texts for the principal's own agent to write from.
The hosted API keeps choosing its builder by key presence.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from chorus import agent_setup
from chorus.audio import MockAudioRenderer
from chorus.bootstrap import AnthropicSoulBuilder, MockSoulBuilder
from chorus.jobs import SqliteJobStore
from chorus.library import LibraryItem, SavedItem
from chorus.library_api import LibraryService
from chorus.llm import MockLLMClient
from chorus.mcp_server import create_mcp_server
from chorus.pipeline import Deps
from chorus.podcasts_api import PodcastDirectory
from chorus.saved_items import SqliteSavedItemStore
from chorus.script import MockScriptComposer
from chorus.soul import validate_soul
from chorus.transcripts import FixtureTranscriptProvider

pytestmark = pytest.mark.usefixtures("chorus_home")

OWNER = "master"
FAKE_KEY = "sk-ant-test-not-a-real-key"
SAVED_AT = datetime(2026, 9, 30, tzinfo=UTC)
TITLES = ["Pricing power in software", "Base rates for founders", "Unit economics of rollups"]


def _store_with_items(db: Path) -> SqliteSavedItemStore:
    store = SqliteSavedItemStore(db)
    items = [
        LibraryItem(provider="readwise", item_kind="document", title=t, saved_at=SAVED_AT)
        for t in TITLES
    ]
    store.put_many(
        [
            SavedItem(
                owner=OWNER,
                key=item.item_key(),
                item=item,
                status="corpus",
                imported_at=SAVED_AT,
                updated_at=SAVED_AT,
            )
            for item in items
        ]
    )
    return store


def _forbid_model_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any Anthropic soul call fails the test, and a key is present so a
    key-presence selector would reach for it."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)

    def _boom(*_: object, **__: object) -> str:
        raise AssertionError("brain=host must not call a model")

    monkeypatch.setattr(AnthropicSoulBuilder, "derive_from_corpus", _boom)
    monkeypatch.setattr(AnthropicSoulBuilder, "build_from_interview", _boom)


def _service(db: Path) -> LibraryService:
    return LibraryService(
        _store_with_items(db),
        PodcastDirectory(),
        soul_builder=agent_setup.library_soul_builder,
    )


def test_host_brain_hands_the_corpus_to_the_agent_without_a_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbid_model_calls(monkeypatch)
    agent_setup.set_choice("mode", "host-agent")
    agent_setup.set_choice("brain", "host")

    soul = _service(tmp_path / "lib.db").soul(OWNER)

    assert soul.based_on == len(TITLES)
    assert sorted(soul.corpus) == sorted(TITLES)
    assert validate_soul(soul.soul).valid
    assert any("source='write'" in note for note in soul.agent_notes)


def test_anthropic_brain_uses_the_anthropic_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    agent_setup.set_choice("brain", "anthropic")
    assert isinstance(agent_setup.library_soul_builder(), AnthropicSoulBuilder)


def test_mock_brain_drafts_from_the_template_and_returns_no_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbid_model_calls(monkeypatch)
    agent_setup.set_choice("brain", "mock")
    assert isinstance(agent_setup.library_soul_builder(), MockSoulBuilder)

    soul = _service(tmp_path / "lib.db").soul(OWNER)

    assert soul.based_on == len(TITLES)
    assert soul.corpus == [] and soul.agent_notes == []


def test_hosted_service_default_is_unchanged(tmp_path: Path) -> None:
    soul = LibraryService(_store_with_items(tmp_path / "lib.db"), PodcastDirectory()).soul(OWNER)
    assert soul.based_on == len(TITLES)
    assert soul.corpus == [] and soul.agent_notes == []


def test_local_mcp_server_follows_the_onboarding_brain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbid_model_calls(monkeypatch)
    agent_setup.set_choice("mode", "host-agent")
    agent_setup.set_choice("brain", "host")
    db = tmp_path / "jobs.db"
    _store_with_items(db).close()
    deps = Deps(
        FixtureTranscriptProvider(), MockLLMClient(), MockScriptComposer(), MockAudioRenderer()
    )
    store = SqliteJobStore(db)

    _, tools = create_mcp_server(store, deps, local=True)
    try:
        soul = tools.soul_from_library()
    finally:
        tools.close()
        store.close()

    assert sorted(soul.corpus) == sorted(TITLES)
