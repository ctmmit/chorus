"""Chapters (chorus/chapters.py): frame walking, chapter timing, source links,
the ID3v2.4 tag, and the audio stage writing them into the stored episode."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from chorus.artifacts import LocalArtifactStore
from chorus.audio import RenderedAudio
from chorus.chapters import (
    MAX_CHAPTERS,
    build_chapters,
    chapters_json,
    mp3_duration_seconds,
    source_link,
    source_url,
    tag_episode,
    write_id3_chapters,
)
from chorus.llm import MockLLMClient
from chorus.models import (
    Chapter,
    Citation,
    Digest,
    DigestRequest,
    EpisodeDigest,
    EpisodeInput,
    EpisodeOutline,
    OutlineSegment,
    Script,
    Take,
    Turn,
)
from chorus.pipeline import stage_audio, stage_curate_episode, stage_ingest, stage_script
from chorus.render_plan import is_mp3, join_mp3
from chorus.script import MockScriptComposer
from chorus.transcripts import FixtureTranscriptProvider

FIX = Path(__file__).resolve().parent.parent / "fixtures"

# MPEG-1 Layer III, 128 kbit/s, 44.1 kHz, no padding: 417-byte frames of
# 1152 samples (26.12 ms each).
FRAME_HEADER = bytes([0xFF, 0xFB, 0x90, 0x00])
FRAME_BYTES = 417
FRAME_SECONDS = 1152 / 44_100


def _mp3(frames: int) -> bytes:
    return (FRAME_HEADER + b"\x00" * (FRAME_BYTES - 4)) * frames


def _synchsafe(raw: bytes) -> int:
    n = 0
    for byte in raw:
        n = (n << 7) | byte
    return n


def _id3_frames(tag: bytes) -> list[tuple[bytes, bytes]]:
    """Top-level (frame id, body) pairs of an ID3v2.4 tag."""
    return _frames(tag[10 : 10 + _synchsafe(tag[6:10])])


def _frames(body: bytes) -> list[tuple[bytes, bytes]]:
    out, i = [], 0
    while i < len(body):
        frame_id = body[i : i + 4]
        n = _synchsafe(body[i + 4 : i + 8])
        out.append((frame_id, body[i + 10 : i + 10 + n]))
        i += 10 + n
    return out


# --- frame walking --------------------------------------------------------


def test_duration_sums_frames() -> None:
    assert mp3_duration_seconds(_mp3(100)) == pytest.approx(100 * FRAME_SECONDS)


def test_duration_skips_leading_tag_junk_and_trailing_id3v1() -> None:
    tag = b"ID3\x04\x00\x00\x00\x00\x00\x05" + b"x" * 5
    data = tag + b"\x00\x01\x02" + _mp3(10) + b"TAG" + b"\x00" * 125
    assert mp3_duration_seconds(data) == pytest.approx(10 * FRAME_SECONDS)


def test_duration_of_joined_chunks_is_the_sum() -> None:
    assert mp3_duration_seconds(join_mp3([_mp3(7), _mp3(5)])) == pytest.approx(12 * FRAME_SECONDS)


def test_duration_of_non_audio_is_zero() -> None:
    assert mp3_duration_seconds(b"not audio at all") == 0.0
    assert mp3_duration_seconds(FRAME_HEADER) == 0.0  # header with no frame body


# --- source links ---------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.youtube.com/watch?v=abc", "https://www.youtube.com/watch?v=abc&t=83s"),
        ("https://youtu.be/abc?t=5", "https://youtu.be/abc?t=83s"),
        ("https://open.spotify.com/episode/xyz?si=1", "https://open.spotify.com/episode/xyz?si=1&t=83"),
        ("https://cdn.example.com/ep/1.mp3?x=1", "https://cdn.example.com/ep/1.mp3?x=1#t=83"),
        (
            "https://podcasts.apple.com/us/podcast/x/id1?i=2",
            "https://podcasts.apple.com/us/podcast/x/id1?i=2",
        ),
    ],
)
def test_source_link_per_host(url: str, expected: str) -> None:
    assert source_link(url, 83.9) == expected


def test_source_link_without_url_or_time() -> None:
    assert source_link(None, 10) is None
    assert source_link("https://youtu.be/abc", None) == "https://youtu.be/abc"


def test_source_url_by_identity() -> None:
    assert source_url(EpisodeInput(video_id="abc")) == "https://www.youtube.com/watch?v=abc"
    assert source_url(EpisodeInput(url="https://youtu.be/abc")) == "https://youtu.be/abc"
    assert source_url(EpisodeInput(audio_url="https://x.com/a.mp3")) == "https://x.com/a.mp3"
    assert source_url(EpisodeInput(feed_url="https://x.com/feed", guid="g")) is None


# --- building chapters ----------------------------------------------------


def _digest(episode_id: str, url: str | None = None) -> EpisodeDigest:
    return EpisodeDigest(
        episode_id=episode_id,
        episode_title=f"Title {episode_id}",
        show="Show",
        highlights=[],
        url=url,
    )


def _outlined_script() -> Script:
    outline = EpisodeOutline(
        segments=[
            OutlineSegment(name="Opening", kind="intro", description="d"),
            OutlineSegment(name="Pricing power", kind="body", source_ids=["a"], description="d"),
            OutlineSegment(name="Close", kind="close", description="d"),
        ]
    )
    turns = [
        Turn(speaker="host", text="x" * 10, segment_index=0),
        Turn(
            speaker="host",
            text="y" * 70,
            segment_index=1,
            episode_id="a",
            segment_timestamp=120.0,
            citations=[Citation(episode_id="a", segment_timestamp=120.0)],
        ),
        Turn(speaker="host", text="z" * 20, segment_index=2),
    ]
    return Script(soul_version="v", takes=[], monologue="", turns=turns, outline=outline)


def test_chapters_follow_outline_segments_by_spoken_share() -> None:
    chapters = build_chapters(
        _outlined_script(), [_digest("a", "https://www.youtube.com/watch?v=a")], 100.0
    )
    assert [c.title for c in chapters] == ["Opening", "Pricing power", "Close"]
    assert [c.start_seconds for c in chapters] == [0.0, 10.0, 80.0]
    assert chapters[0].url is None
    assert chapters[1].episode_id == "a"
    assert chapters[1].source_timestamp == 120.0
    assert chapters[1].url == "https://www.youtube.com/watch?v=a&t=120s"


def test_chapters_without_outline_group_by_source() -> None:
    script = Script(
        soul_version="v",
        takes=[
            Take(text="a" * 10, take_type="t", episode_id="a", segment_timestamp=5.0),
            Take(text="a" * 10, take_type="t", episode_id="a", segment_timestamp=9.0),
            Take(text="b" * 20, take_type="t", episode_id="b", segment_timestamp=30.0),
        ],
        monologue="",
    )
    chapters = build_chapters(script, [_digest("a"), _digest("b")], 40.0)
    assert [c.title for c in chapters] == ["Show: Title a", "Show: Title b"]
    assert [c.start_seconds for c in chapters] == [0.0, 20.0]
    assert chapters[0].source_timestamp == 5.0


def test_uncited_opening_lines_get_an_intro_chapter() -> None:
    script = Script(
        soul_version="v",
        takes=[],
        monologue="",
        turns=[
            Turn(speaker="host", text="Welcome back."),
            Turn(speaker="host", text="First source.", episode_id="a", segment_timestamp=1.0),
        ],
    )
    chapters = build_chapters(script, [_digest("a")], 10.0)
    assert [c.title for c in chapters] == ["Intro", "Show: Title a"]


def test_no_chapters_without_duration_or_speech() -> None:
    assert build_chapters(_outlined_script(), [], 0.0) == []
    empty = Script(soul_version="v", takes=[], monologue="")
    assert build_chapters(empty, [], 50.0) == []


def test_chapters_are_capped_for_the_table_of_contents() -> None:
    turns = [
        Turn(speaker="host", text="line", episode_id=f"e{i}", segment_timestamp=0.0)
        for i in range(MAX_CHAPTERS + 5)
    ]
    script = Script(soul_version="v", takes=[], monologue="", turns=turns)
    digests = [_digest(f"e{i}") for i in range(MAX_CHAPTERS + 5)]
    assert len(build_chapters(script, digests, 1000.0)) == MAX_CHAPTERS


# --- the ID3 tag ----------------------------------------------------------


def test_id3_tag_round_trips() -> None:
    audio = _mp3(20)
    chapters = [
        Chapter(start_seconds=0.0, title="Opening"),
        Chapter(start_seconds=0.25, title="Café", url="https://youtu.be/a?t=3s"),
    ]
    tagged = write_id3_chapters(audio, chapters, 0.5, "Chorus")
    assert tagged.startswith(b"ID3\x04\x00")
    assert tagged.endswith(audio) and is_mp3(tagged)
    assert mp3_duration_seconds(tagged) == pytest.approx(20 * FRAME_SECONDS)

    frames = _id3_frames(tagged)
    assert [f[0] for f in frames] == [b"TIT2", b"CTOC", b"CHAP", b"CHAP"]
    assert frames[0][1] == b"\x03Chorus"
    assert frames[1][1] == b"toc\x00\x03\x02ch0\x00ch1\x00"
    second = frames[3][1]
    assert second.startswith(b"ch1\x00")
    start, end = int.from_bytes(second[4:8], "big"), int.from_bytes(second[8:12], "big")
    assert (start, end) == (250, 500)
    sub = _frames(second[20:])
    assert sub[0] == (b"TIT2", "\x03Café".encode())
    assert sub[1] == (b"WXXX", b"\x03\x00https://youtu.be/a?t=3s")


def test_existing_tag_is_replaced_not_stacked() -> None:
    once = write_id3_chapters(_mp3(5), [Chapter(start_seconds=0, title="A")], 1.0, "Chorus")
    twice = write_id3_chapters(once, [Chapter(start_seconds=0, title="B")], 1.0, "Chorus")
    assert twice.count(b"ID3") == 1
    assert twice.endswith(_mp3(5))


def test_tag_episode_leaves_unwalkable_audio_alone() -> None:
    result = tag_episode(b"not audio", _outlined_script(), [])
    assert result.data == b"not audio"
    assert result.chapters == []


def test_chapters_json_shape() -> None:
    doc = json.loads(
        chapters_json(
            [Chapter(start_seconds=0, title="A"), Chapter(start_seconds=5, title="B", url="u")]
        )
    )
    assert doc["version"] == "1.2.0"
    assert doc["chapters"] == [
        {"startTime": 0, "title": "A"},
        {"startTime": 5, "title": "B", "url": "u"},
    ]


# --- the audio stage ------------------------------------------------------


class _Mp3Renderer:
    def __init__(self, frames: int) -> None:
        self.frames = frames

    def render(self, script: Script, soul: str, job_id: str) -> RenderedAudio:
        return RenderedAudio(data=_mp3(self.frames), media_type="audio/mpeg", extension="mp3")


def _digest_and_script() -> tuple[DigestRequest, Digest, Script]:
    request = DigestRequest(
        soul=(FIX / "souls" / "soul_investor.md").read_text(encoding="utf-8"),
        context="",
        episodes=[EpisodeInput(video_id="sample_public")],
    )
    ingested = stage_ingest(request, FixtureTranscriptProvider())
    episode = stage_curate_episode(ingested.resolved[0], request, MockLLMClient()).digest
    digest = Digest(soul_version="v", episodes=[episode])
    return request, digest, stage_script(digest, request, MockScriptComposer())


def test_stage_audio_writes_one_chapter_per_outline_segment(tmp_path: Path) -> None:
    request, digest, script = _digest_and_script()
    assert script.outline is not None
    assert all(t.segment_index is not None for t in script.turns)
    assert digest.episodes[0].url == "https://www.youtube.com/watch?v=sample_public"

    result = stage_audio(
        script, request, "job-ch1", _Mp3Renderer(400), LocalArtifactStore(tmp_path), digest
    )

    spoken = {t.segment_index for t in script.turns if t.text.strip()}
    assert len(result.chapters) == len(spoken)
    assert result.chapters[0].start_seconds == 0.0
    assert any(c.url and "&t=" in c.url for c in result.chapters)
    stored = (tmp_path / "episode_job-ch1.mp3").read_bytes()
    assert stored.startswith(b"ID3\x04") and b"CHAP" in stored and b"CTOC" in stored
    assert stored.endswith(_mp3(400))


def test_stage_audio_does_not_tag_placeholder_text(tmp_path: Path) -> None:
    from chorus.audio import MockAudioRenderer

    request, digest, script = _digest_and_script()
    result = stage_audio(
        script, request, "job-ch2", MockAudioRenderer(), LocalArtifactStore(tmp_path), digest
    )
    assert result.chapters == []
    assert (tmp_path / "episode_job-ch2.txt").read_text(encoding="utf-8") == script.monologue
