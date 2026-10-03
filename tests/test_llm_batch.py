"""Batched scoring: the real client's request shape and its parser, exercised
against a fake Anthropic client so no key and no network are needed."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from chorus.llm import (
    BATCH_WINDOWS,
    EXCERPT_SCORE_FLOOR,
    UNSCORED_REASON,
    AnthropicLLMClient,
    LLMError,
    MockLLMClient,
    _parse_batch,
)


@dataclass
class _Block:
    type: str
    text: str


@dataclass
class _Usage:
    input_tokens: int = 100
    output_tokens: int = 20
    cache_read_input_tokens: int = 80
    cache_creation_input_tokens: int = 0


@dataclass
class _Msg:
    content: list[_Block]
    usage: _Usage = field(default_factory=_Usage)


class _FakeMessages:
    """Replays canned reply bodies in order and records every request."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []

    def create(self, **kw: Any) -> _Msg:
        self.requests.append(kw)
        return _Msg(content=[_Block("text", self.replies.pop(0))])


class _FakeClient:
    def __init__(self, replies: list[str]) -> None:
        self.messages = _FakeMessages(replies)


def _reply(n: int, score: float = 0.9) -> str:
    return json.dumps([{"i": i, "score": score, "reason": f"r{i}"} for i in range(n)])


def test_batch_makes_one_call_with_cached_system_block() -> None:
    fake = _FakeClient([_reply(3)])
    client = AnthropicLLMClient(client=fake)
    out = client.score_windows(["a", "b", "c"], soul="SOUL", context="CTX")

    assert out == [(0.9, "r0"), (0.9, "r1"), (0.9, "r2")]
    assert len(fake.messages.requests) == 1
    req = fake.messages.requests[0]
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "SOUL" in req["system"][0]["text"] and "CTX" in req["system"][0]["text"]
    assert "[2]\nc" in req["messages"][0]["content"]
    assert client.usage.calls == 1 and client.usage.cache_read_tokens == 80


def test_batch_chunks_long_episodes() -> None:
    n = BATCH_WINDOWS + 5
    fake = _FakeClient([_reply(BATCH_WINDOWS), _reply(5)])
    out = AnthropicLLMClient(client=fake).score_windows([f"w{i}" for i in range(n)], "s", "c")
    assert len(out) == n
    assert len(fake.messages.requests) == 2


def test_prose_around_json_is_tolerated() -> None:
    body = "Sure, here are the scores:\n" + _reply(2) + "\nHope that helps."
    assert _parse_batch(body, 2) == [(0.9, "r0"), (0.9, "r1")]


def test_missing_few_indices_are_zero_scored() -> None:
    body = json.dumps([{"i": 0, "score": 0.7, "reason": "x"}, {"i": 2, "score": 0.4, "reason": "y"}])
    assert _parse_batch(body, 3) == [(0.7, "x"), (0.0, UNSCORED_REASON), (0.4, "y")]


def test_scores_are_clamped_and_bad_items_skipped() -> None:
    body = json.dumps([{"i": 0, "score": 7, "reason": "hot"}, {"i": "x"}, {"i": 1, "score": -1}])
    assert _parse_batch(body, 2) == [(1.0, "hot"), (0.0, "no reason")]


def test_unparseable_reply_retries_once_then_raises() -> None:
    fake = _FakeClient(["nonsense", "still nonsense"])
    with pytest.raises(LLMError):
        AnthropicLLMClient(client=fake).score_windows(["a"], "s", "c")
    assert len(fake.messages.requests) == 2


def test_mostly_unscored_batch_raises() -> None:
    body = json.dumps([{"i": 0, "score": 0.5, "reason": "only one"}])
    with pytest.raises(LLMError):
        _parse_batch(body, 4)


# --- R25: reject non-finite scores instead of clamping them -----------


def test_non_standard_json_constant_score_is_rejected_not_clamped() -> None:
    # json.dumps can't emit NaN/Infinity itself (they aren't valid JSON), but
    # Python's decoder accepts the literal tokens by default — exactly what
    # chorus.llm._reject_non_finite_constant must refuse.
    body = '[{"i": 0, "score": NaN, "reason": "bad"}]'
    with pytest.raises(LLMError):
        _parse_batch(body, 1)


def test_overflowing_json_number_score_is_treated_as_unscored_not_clamped_to_one() -> None:
    # "1e400" is a syntactically ordinary JSON number that float() overflows
    # to +inf — math.isfinite must still catch it even though
    # parse_constant never sees it as a special token. n=3 with one bad item
    # stays under the "mostly unscored" retry threshold (see
    # test_missing_few_indices_are_zero_scored), isolating this assertion to
    # the finite-score check rather than the missing-count one.
    body = (
        '[{"i": 0, "score": 1e400, "reason": "huge"}, '
        '{"i": 1, "score": 0.5, "reason": "b"}, {"i": 2, "score": 0.5, "reason": "c"}]'
    )
    assert _parse_batch(body, 3) == [(0.0, UNSCORED_REASON), (0.5, "b"), (0.5, "c")]


def test_negative_infinity_score_is_treated_as_unscored() -> None:
    body = (
        '[{"i": 0, "score": -1e400, "reason": "huge"}, '
        '{"i": 1, "score": 0.5, "reason": "b"}, {"i": 2, "score": 0.5, "reason": "c"}]'
    )
    assert _parse_batch(body, 3) == [(0.0, UNSCORED_REASON), (0.5, "b"), (0.5, "c")]


def test_single_segment_path_still_parses() -> None:
    fake = _FakeClient(["SCORE: 0.85\nREASON: names a mechanism"])
    assert AnthropicLLMClient(client=fake).score_segment("t", "s", "c") == (0.85, "names a mechanism")


def test_mock_batch_matches_single() -> None:
    m = MockLLMClient()
    soul = "## Attention triggers\n- margins and moats\n## Ignore\n- celebrity"
    texts = ["margins expand and moats widen", "celebrity gossip", "nothing"]
    out = m.score_windows(texts, soul, "")
    assert [r[:2] for r in out] == [m.score_segment(t, soul, "") for t in texts]


def test_mock_excerpt_is_the_best_matching_sentence_or_nothing() -> None:
    m = MockLLMClient()
    soul = "## Attention triggers\n- margins and moats\n## Ignore\n- celebrity"
    window = "Welcome back to the show. Margins expand and moats widen. A celebrity spoke."
    out = m.score_windows([window, "celebrity gossip only", "nothing here"], soul, "")
    assert out[0] == (*m.score_segment(window, soul, ""), "Margins expand and moats widen.")
    assert len(out[1]) == 2 and len(out[2]) == 2  # no sentence nets a match: no excerpt
    assert m.score_windows([window], soul, "") == out[:1]  # deterministic


def test_parse_batch_carries_an_excerpt_when_present() -> None:
    body = json.dumps(
        [
            {"i": 0, "score": 0.8, "reason": "a", "excerpt": "the span that earned it"},
            {"i": 1, "score": 0.1, "reason": "b", "excerpt": "   "},
            {"i": 2, "score": 0.1, "reason": "c", "excerpt": 42},
            {"i": 3, "score": 0.1, "reason": "d"},
        ]
    )
    assert _parse_batch(body, 4) == [
        (0.8, "a", "the span that earned it"),
        (0.1, "b"),
        (0.1, "c"),
        (0.1, "d"),
    ]


def test_batch_prompt_asks_for_a_verbatim_excerpt_above_the_bar() -> None:
    fake = _FakeClient([_reply(1)])
    AnthropicLLMClient(client=fake).score_windows(["a"], "s", "c")
    prompt = fake.messages.requests[0]["messages"][0]["content"]
    assert '"excerpt"' in prompt
    assert f"{EXCERPT_SCORE_FLOOR} or higher" in prompt
    assert "verbatim" in prompt


def test_excerpt_floor_tracks_the_relevance_threshold() -> None:
    from chorus.curation import RELEVANCE_THRESHOLD

    assert EXCERPT_SCORE_FLOOR == RELEVANCE_THRESHOLD


def test_job_usage_records_token_delta_per_job(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Two jobs share one metered client; each job must report only its own spend."""
    from pathlib import Path

    from fastapi.testclient import TestClient

    from chorus.app import create_app
    from chorus.artifacts import LocalArtifactStore
    from chorus.audio import MockAudioRenderer
    from chorus.jobs import SqliteJobStore
    from chorus.pipeline import Deps
    from chorus.script import MockScriptComposer
    from chorus.transcripts import FixtureTranscriptProvider

    fix = Path(__file__).resolve().parent.parent / "fixtures"
    soul = (fix / "souls" / "soul_investor.md").read_text(encoding="utf-8")
    # sample_public has 3 windows -> one batch per job; canned replies for two jobs.
    fake = _FakeClient([_reply(3), _reply(3)])
    deps = Deps(FixtureTranscriptProvider(), AnthropicLLMClient(client=fake), MockScriptComposer(),
                MockAudioRenderer(out_dir=tmp_path / "artifacts"),
                LocalArtifactStore(tmp_path / "artifacts"))
    c = TestClient(create_app(SqliteJobStore(tmp_path / "jobs.db"), deps))
    payload = {"soul": soul, "context": "", "episodes": [{"video_id": "sample_public"}]}
    for _ in range(2):
        job_id = c.post("/digest", json=payload).json()["job_id"]
        body = c.get(f"/digest/{job_id}").json()
        assert body["status"] == "done"
        assert body["usage"]["llm_tokens"] == {
            "calls": 1, "input_tokens": 100, "output_tokens": 20,
            "cache_read_tokens": 80, "cache_write_tokens": 0,
        }
