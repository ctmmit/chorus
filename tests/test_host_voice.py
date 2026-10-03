"""Agent voice ("host-plugin"): the principal's agent renders the episode with
its own text-to-speech tool, and Chorus plans the chunks and joins the result.

The tests play the agent's TTS tool by writing small synthetic MP3 files
(an MPEG frame header plus padding, optionally behind an ID3v2 tag). Nothing
calls ElevenLabs.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

import pytest

from chorus import agent_setup, host_mode
from chorus.cli import EXIT_OK
from chorus.cli import main as cli_main
from chorus.curation import window_segments
from chorus.host_mode import HostModeError
from chorus.jobs import SqliteJobStore
from chorus.mcp_setup import submit_configured_digest
from chorus.models import EpisodeInput, JobStatus, Script, Turn
from chorus.onboarding import Mode, OnboardingConfig, Step, Voice, load_config, options
from chorus.render_plan import (
    MAX_CHUNK_CHARS,
    AudioChunkError,
    build_plan,
    is_mp3,
    join_mp3,
    read_chunk,
)
from chorus.transcripts import FixtureTranscriptProvider

pytestmark = pytest.mark.usefixtures("chorus_home")

SAMPLE = "sample_public"
FRAME = b"\xff\xfb\x90\x64" + b"\x00" * 413
INTERVIEW = {
    "identity": "Operator allocating capital",
    "interests": "unit economics, pricing",
    "triggers": "moats, base rates",
    "ignore": "celebrity",
    "style": "empirical",
    "guidance": "Only falsifiable claims; refuse otherwise",
}


def _id3(payload: bytes = b"TIT2tagdata") -> bytes:
    size = len(payload)
    synchsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])
    return b"ID3\x04\x00\x00" + synchsafe + payload


def _mp3(n: int, tagged: bool = True) -> bytes:
    body = FRAME + bytes([n % 256]) * 8
    return (_id3() + body) if tagged else body


@pytest.fixture
def store(chorus_home: Path) -> Any:
    from chorus import paths

    jobs = SqliteJobStore(paths.db_path())
    yield jobs
    jobs.close()


def _setup(brain: str = "host", voice: str = "host-plugin") -> OnboardingConfig:
    agent_setup.set_choice("mode", "host-agent")
    agent_setup.set_choice("brain", brain)
    agent_setup.set_choice("voice", voice)
    agent_setup.set_choice("transcripts", "free")
    agent_setup.soul_save("me", agent_setup.soul_draft("interview", answers=INTERVIEW).markdown)
    agent_setup.set_shows([], [], weekly=False)
    agent_setup.set_choice("updates", "notify")
    return load_config()


def _window_count() -> int:
    transcript = FixtureTranscriptProvider().get(EpisodeInput(video_id=SAMPLE))
    return len(window_segments(transcript.segments))


def _to_render(store: Any, episode_format: str = "monologue") -> str:
    """Host brain + agent voice: score and script, arriving at the render task."""
    _setup()
    job_id = host_mode.start(
        store,
        load_config(),
        [EpisodeInput(video_id=SAMPLE)],
        episode_format=episode_format,
        background=False,
    )
    n = _window_count()
    scores = [{"i": i, "score": 0.9 if i in (0, n - 1) else 0.05, "reason": "r"} for i in range(n)]
    host_mode.submit_scores(store, job_id, SAMPLE, scores)
    takes = [{"ref": 0, "take_type": "idea", "text": "Cost curve."}]
    turns = [
        {"ref": 0, "speaker": "host", "text": "Margins went from forty to seventy."},
        {"ref": 1, "speaker": "cohost", "text": "Only for the licensees."},
    ]
    host_mode.submit_script(store, load_config(), job_id, takes, turns)
    return job_id


def _voice_plan(task: dict[str, Any], tmp_path: Path) -> list[dict[str, Any]]:
    """What the agent's TTS tool does: one MP3 file per chunk."""
    out = []
    for chunk in task["render_plan"]["chunks"]:
        file = tmp_path / f"tts_{chunk['index']}.mp3"
        file.write_bytes(_mp3(chunk["index"]))
        out.append({"index": chunk["index"], "path": str(file)})
    return out


# --- render plan -----------------------------------------------------------


def _script(**kwargs: Any) -> Script:
    return Script(soul_version="v", takes=[], **kwargs)


def test_monologue_plan_is_one_host_chunk_with_default_voice() -> None:
    plan = build_plan(_script(monologue="One short take."))
    assert [(c.role, c.text) for c in plan.chunks] == [("host", "One short take.")]
    assert plan.chunks[0].voice_id and plan.output_format.startswith("mp3")


def test_long_monologue_splits_on_sentences_under_the_limit() -> None:
    sentence = "This is a sentence about margins and moats. "
    text = (sentence * 120).strip()
    plan = build_plan(_script(monologue=text))
    assert len(plan.chunks) > 1
    assert all(len(c.text) <= MAX_CHUNK_CHARS for c in plan.chunks)
    assert " ".join(c.text for c in plan.chunks).split() == text.split()


def test_dialogue_plan_merges_same_speaker_and_honors_voices() -> None:
    turns = [
        Turn(speaker="host", text="A.", episode_id="e", segment_timestamp=0),
        Turn(speaker="host", text="B.", episode_id="e", segment_timestamp=0),
        Turn(speaker="cohost", text="C.", episode_id="e", segment_timestamp=0),
    ]
    script = _script(monologue="x", turns=turns, format="dialogue", voices={"cohost": "voice-c"})
    plan = build_plan(script, env_voices={"host": "voice-h", "cohost": "ignored"})
    assert [(c.role, c.text, c.voice_id) for c in plan.chunks] == [
        ("host", "A. B.", "voice-h"),
        ("cohost", "C.", "voice-c"),  # the profile's voice beats the env default
    ]


def test_mp3_detection_and_joining_strips_inner_tags() -> None:
    assert is_mp3(_mp3(0)) and is_mp3(_mp3(0, tagged=False))
    assert not is_mp3(b"Success. File saved as: x.mp3")
    joined = join_mp3([_mp3(1), _mp3(2)])
    assert joined.startswith(b"ID3")
    assert joined.count(b"ID3") == 1
    assert joined.endswith(_mp3(2, tagged=False))


def test_read_chunk_validates_input(tmp_path: Path) -> None:
    good = tmp_path / "a.mp3"
    good.write_bytes(_mp3(0))
    assert read_chunk(path=str(good)) == _mp3(0)
    assert read_chunk(data_base64=base64.b64encode(_mp3(0)).decode()) == _mp3(0)
    text = tmp_path / "notes.txt"
    text.write_text("my private notes", encoding="utf-8")
    with pytest.raises(AudioChunkError, match="not an MP3"):
        read_chunk(path=str(text))
    with pytest.raises(AudioChunkError, match="no file"):
        read_chunk(path=str(tmp_path / "missing.mp3"))
    with pytest.raises(AudioChunkError, match="exactly one"):
        read_chunk()
    with pytest.raises(AudioChunkError, match="base64"):
        read_chunk(data_base64="!!!")


# --- configuration ---------------------------------------------------------


def test_agent_voice_needs_host_agent_mode() -> None:
    local = {o.value: o for o in options(Step.voice, OnboardingConfig(mode=Mode.local_agent))}
    hosted = {o.value: o for o in options(Step.voice, OnboardingConfig(mode=Mode.host_agent))}
    assert not local[Voice.host_plugin].available
    assert hosted[Voice.host_plugin].available


def test_voice_step_tells_the_agent_to_check_for_a_tts_tool() -> None:
    agent_setup.set_choice("mode", "host-agent")
    agent_setup.set_choice("brain", "host")
    prompt = agent_setup.agent_status().next
    assert prompt is not None and prompt.step is Step.voice
    assert any("elevenlabs-mcp" in n for n in prompt.agent_notes)


# --- host brain + agent voice ----------------------------------------------


def test_script_leads_to_a_render_task(store: Any) -> None:
    job_id = _to_render(store)
    task = host_mode.next_task(store, job_id)
    assert task.kind == "render"
    assert task.render_plan and task.render_plan["chunks"]
    assert "host_submit_audio" in task.instructions
    assert store.get(job_id).status is JobStatus.digest_ready


def test_voiced_chunks_become_the_episode(store: Any, tmp_path: Path) -> None:
    job_id = _to_render(store, "dialogue")
    task = host_mode.next_task(store, job_id).model_dump(mode="json")
    assert [c["role"] for c in task["render_plan"]["chunks"]] == ["host", "cohost"]
    assert "turn by turn" in task["instructions"]
    done = host_mode.submit_audio(store, job_id, _voice_plan(task, tmp_path))["next"]
    assert done["kind"] == "done"
    episode = Path(done["result"]["episode_file"])
    assert episode.suffix == ".mp3"
    assert episode.read_bytes() == join_mp3([_mp3(0), _mp3(1)])
    assert store.get(job_id).status is JobStatus.done


def test_missing_or_bad_chunks_keep_the_run_open(store: Any, tmp_path: Path) -> None:
    job_id = _to_render(store, "dialogue")
    task = host_mode.next_task(store, job_id).model_dump(mode="json")
    voiced = _voice_plan(task, tmp_path)
    with pytest.raises(HostModeError, match="missing chunks"):
        host_mode.submit_audio(store, job_id, voiced[:1])
    with pytest.raises(HostModeError, match="submitted twice"):
        host_mode.submit_audio(store, job_id, [voiced[0], voiced[0]])
    Path(voiced[1]["path"]).write_text("Success. File saved as: elsewhere.mp3", encoding="utf-8")
    with pytest.raises(HostModeError, match="chunk 1: not an MP3"):
        host_mode.submit_audio(store, job_id, voiced)
    assert host_mode.next_task(store, job_id).kind == "render"


def test_skipping_audio_still_delivers_the_digest(store: Any) -> None:
    job_id = _to_render(store)
    done = host_mode.submit_audio(store, job_id, skip_reason="no text-to-speech tool")["next"]
    assert done["kind"] == "done"
    assert Path(done["result"]["episode_file"]).suffix == ".txt"
    assert any("audio skipped by the agent" in w for w in done["result"]["warnings"])


# --- Chorus brain + agent voice --------------------------------------------


def test_chorus_brain_with_agent_voice_goes_straight_to_render(
    store: Any, tmp_path: Path
) -> None:
    _setup(brain="mock")
    job_id = host_mode.start(
        store, load_config(), [EpisodeInput(video_id=SAMPLE)], background=False
    )
    task = host_mode.next_task(store, job_id).model_dump(mode="json")
    assert task["kind"] == "render"  # no score or script task: Chorus did the thinking
    job = store.get(job_id)
    assert job.status is JobStatus.digest_ready and job.script is not None
    assert job.usage.brain == "mock"
    done = host_mode.submit_audio(store, job_id, _voice_plan(task, tmp_path))["next"]
    assert done["result"]["status"] == "done"


def test_run_my_digest_asks_the_agent_to_drive(store: Any) -> None:
    _setup(brain="mock")
    started = submit_configured_digest(store, [SAMPLE])
    assert started["drive"] == "host_next" and started["brain"] == "mock"
    # Let the background pipeline finish inside this test's isolated home.
    import time

    deadline = time.monotonic() + 20
    while host_mode.next_task(store, started["job_id"]).kind == "wait":
        assert time.monotonic() < deadline
        time.sleep(0.05)


def test_smoke_test_with_agent_voice_completes_after_render(store: Any, tmp_path: Path) -> None:
    _setup(brain="mock")
    started = agent_setup.smoke_test(run=True, background=False)
    task = started["next"]
    assert task["kind"] == "render"
    host_mode.submit_audio(store, started["host_job_id"], _voice_plan(task, tmp_path))
    assert agent_setup.agent_status().next is None
    assert Step.smoke_test in load_config().completed


def test_cli_host_audio(
    store: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    job_id = _to_render(store)
    task = host_mode.next_task(store, job_id).model_dump(mode="json")
    chunks = tmp_path / "chunks.json"
    chunks.write_text(json.dumps({"chunks": _voice_plan(task, tmp_path)}), encoding="utf-8")
    assert cli_main(["setup", "host-audio", job_id, "--file", str(chunks)]) == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["next"]["result"]["status"] == "done"
    assert os.path.splitext(out["next"]["result"]["episode_file"])[1] == ".mp3"
