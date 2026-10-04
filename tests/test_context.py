"""Structured context (chorus/context.py, DigestRequest.context_blocks):
rendering, the shared budget, round-trips, providers, and the job record."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from chorus.context import (
    ITEM_MAX_CHARS,
    MockContextProvider,
    ReadwiseContextProvider,
    block_summary,
    render_context,
)
from chorus.jobs import SqliteJobStore
from chorus.models import (
    MAX_CONTEXT_BLOCKS,
    ContextBlock,
    ContextItem,
    DigestRequest,
    EpisodeInput,
)

ROOT = Path(__file__).resolve().parent.parent
SOUL = (ROOT / "fixtures" / "souls" / "soul_investor.md").read_text(encoding="utf-8")
AS_OF = datetime(2026, 10, 4, tzinfo=UTC)


def _block(source: str, *texts: str, label: str | None = None) -> ContextBlock:
    return ContextBlock(
        source=source, items=[ContextItem(text=t, label=label) for t in texts], as_of=AS_OF
    )


# --- rendering ---------------------------------------------------------------------


def test_free_text_comes_first_then_each_block() -> None:
    text, sources = render_context(
        "Building Fulcrum.",
        [_block("Readwise highlights", "Inference got 10x cheaper.", label="AI Economics"),
         _block("Active projects", "Fulcrum beta this month")],
    )
    assert text == (
        "Building Fulcrum.\n\n"
        "## Readwise highlights, as of 04 Oct 2026\n- Inference got 10x cheaper. (AI Economics)\n\n"
        "## Active projects, as of 04 Oct 2026\n- Fulcrum beta this month"
    )
    assert sources == ["Readwise highlights", "Active projects"]


def test_long_items_are_trimmed_and_blocks_share_the_budget() -> None:
    long = "x" * (ITEM_MAX_CHARS * 2)
    reading = _block("Reading", *[f"highlight {i} " + "y" * 60 for i in range(50)])
    projects = _block("Projects", "Fulcrum", long)
    text, sources = render_context("", [reading, projects], budget=1_500)
    assert len(text) <= 1_500
    assert sources == ["Reading", "Projects"]  # the long reading list did not crowd projects out
    assert "- Fulcrum" in text
    trimmed = next(line for line in text.splitlines() if line.startswith("- xxx"))
    assert len(trimmed) <= ITEM_MAX_CHARS + 2 and trimmed.endswith("...")


def test_empty_blocks_contribute_nothing() -> None:
    text, sources = render_context("ctx", [ContextBlock(source="Calendar", items=[])])
    assert (text, sources) == ("ctx", [])


# --- the request -----------------------------------------------------------------------


def _request(**kw: object) -> DigestRequest:
    return DigestRequest(soul=SOUL, context="Free text.", episodes=[EpisodeInput(video_id="a")], **kw)  # type: ignore[arg-type]


def test_blocks_render_once_and_survive_a_round_trip() -> None:
    request = _request(context_blocks=[_block("Tasks", "Ship the podcast feed")])
    assert request.context_blocks == []
    assert request.context_sources == ["Tasks"]
    assert "## Tasks" in request.context and request.context.startswith("Free text.")
    again = DigestRequest.model_validate_json(request.model_dump_json())
    assert again.context == request.context  # not rendered twice (Inngest round-trips requests)
    assert again.context_sources == ["Tasks"]


def test_request_bounds() -> None:
    with pytest.raises(ValidationError):
        _request(context_blocks=[_block(f"s{i}", "x") for i in range(MAX_CONTEXT_BLOCKS + 1)])
    with pytest.raises(ValidationError):
        ContextItem(text="")
    with pytest.raises(ValidationError):
        ContextBlock(source="", items=[])


def test_job_records_the_context_sources(tmp_path: Path) -> None:
    from chorus.artifacts import LocalArtifactStore
    from chorus.audio import MockAudioRenderer
    from chorus.llm import MockLLMClient
    from chorus.pipeline import Deps, run_job
    from chorus.script import MockScriptComposer
    from chorus.transcripts import FixtureTranscriptProvider

    store = SqliteJobStore(tmp_path / "jobs.db")
    deps = Deps(
        FixtureTranscriptProvider(), MockLLMClient(), MockScriptComposer(),
        MockAudioRenderer(out_dir=tmp_path), LocalArtifactStore(tmp_path),
    )
    job_id = store.create()
    request = DigestRequest(
        soul=SOUL, context="", episodes=[EpisodeInput(video_id="sample_public")],
        context_blocks=[_block("Readwise highlights", "unit economics of inference")],
    )
    run_job(job_id, request, store, deps)
    job = store.get(job_id)
    assert job is not None and job.usage is not None
    assert job.usage.context_sources == ["Readwise highlights"]


# --- providers ---------------------------------------------------------------------------


def test_mock_provider_returns_its_items() -> None:
    items = [ContextItem(text="a")]
    block = MockContextProvider("Notes", items).fetch(AS_OF)
    assert (block.source, block.items, block.as_of) == ("Notes", items, AS_OF)


def test_readwise_provider_reads_the_export_api() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = {"results": [
            {"title": "AI Economics", "highlights": [{"text": "Inference got cheaper."},
                                                     {"text": "  "}]},
            {"title": "", "highlights": [{"text": "Untitled highlight."}]},
        ]}
        return httpx.Response(200, content=json.dumps(body))

    provider = ReadwiseContextProvider("tok", transport=httpx.MockTransport(handler))
    block = provider.fetch(AS_OF)
    provider.close()
    assert seen[0].headers["Authorization"] == "Token tok"
    assert seen[0].url.params["updatedAfter"] == AS_OF.isoformat()
    assert [(i.text, i.label) for i in block.items] == [
        ("Inference got cheaper.", "AI Economics"), ("Untitled highlight.", None),
    ]
    assert block_summary([block]) == [{"source": "Readwise highlights", "items": 2}]


def test_the_skill_documents_the_contract() -> None:
    skill = (ROOT / "skills" / "chorus-context" / "SKILL.md").read_text(encoding="utf-8")
    assert skill.startswith("---\nname: chorus-context\n")
    for needle in ("context_blocks", "usage.context_sources", "## Never include"):
        assert needle in skill
