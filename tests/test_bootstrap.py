"""Goal 5 — soul bootstrap (source-agnostic). The derived/interview soul must be
a real, working lens, and the lens must actually condition the output (a finance
corpus surfaces finance highlights; a cooking corpus refuses on a finance show).
"""
from __future__ import annotations

from chorus.bootstrap import MockSoulBuilder
from chorus.curation import build_digest, curate_episode
from chorus.ingest import ingest
from chorus.llm import MockLLMClient
from chorus.models import EpisodeInput
from chorus.transcripts import FixtureTranscriptProvider

ANDREESSEN = "c4tvVKDhpiY"
SECTIONS = (
    "Identity & Role",
    "Core Interests",
    "Attention Triggers",
    "Anti-interests",
    "Taste & Sensibility",
    "Curation Guidance",
)
FINANCE_CORPUS = [
    "venture capital and startup investment in technology companies",
    "market valuation, fund returns, growth economics and AI",
    "founders, capital allocation, and company business models",
]
COOKING_CORPUS = [
    "recipe with butter eggs and flour, whisk and bake in the oven",
    "delicious kitchen techniques, salt and heat in the pan",
]


def _resolve(video_id: str):
    return ingest([EpisodeInput(video_id=video_id)], FixtureTranscriptProvider()).resolved[0]


def test_corpus_derive_has_all_sections() -> None:
    soul = MockSoulBuilder().derive_from_corpus(FINANCE_CORPUS)
    for section in SECTIONS:
        assert section in soul


def test_interview_build_reflects_answers() -> None:
    soul = MockSoulBuilder().build_from_interview(
        {"identity": "A quant", "interests": "options, volatility", "ignore": "celebrity gossip"}
    )
    for section in SECTIONS:
        assert section in soul
    assert "options" in soul
    assert "celebrity gossip" in soul


def test_derived_finance_soul_is_a_working_lens() -> None:
    soul = MockSoulBuilder().derive_from_corpus(FINANCE_CORPUS)
    ep = curate_episode(_resolve(ANDREESSEN), soul, "", MockLLMClient())
    assert not ep.refused
    assert ep.highlights, "a finance-derived soul should surface finance highlights"


def test_offtopic_corpus_soul_refuses_on_finance_show() -> None:
    soul = MockSoulBuilder().derive_from_corpus(COOKING_CORPUS)
    ep = curate_episode(_resolve(ANDREESSEN), soul, "", MockLLMClient())
    assert ep.refused  # the lens genuinely conditions the output


def test_derived_soul_origin_flows_to_digest() -> None:
    soul = MockSoulBuilder().derive_from_corpus(FINANCE_CORPUS)
    ig = ingest([EpisodeInput(video_id=ANDREESSEN)], FixtureTranscriptProvider())
    digest = build_digest(ig, soul, "", MockLLMClient(), soul_origin="derived:corpus")
    assert digest.soul_origin == "derived:corpus"
