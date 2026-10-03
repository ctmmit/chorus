"""Host brain: the principal's agent scores and scripts; Chorus validates.

The tests play the host agent with hand-written scores and takes over the
bundled synthetic transcript. Nothing calls a model. The properties that
matter: quotes always come from the transcript (never the agent), refusals
survive, ungrounded script items are dropped, and the run is driven entirely
by `next_task`.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from chorus import agent_setup, host_mode
from chorus.cli import EXIT_OK
from chorus.cli import main as cli_main
from chorus.curation import REFUSAL, citation_resolves, window_segments
from chorus.host_mode import EMPTY_EPISODE_TEXT, RUBRIC_VERSION, HostModeError
from chorus.jobs import SqliteJobStore
from chorus.mcp_setup import submit_configured_digest
from chorus.models import EpisodeInput, JobStatus, Transcript
from chorus.onboarding import Brain, Mode, OnboardingConfig, Step, load_config, options
from chorus.transcripts import FixtureTranscriptProvider

pytestmark = pytest.mark.usefixtures("chorus_home")

SAMPLE = "sample_public"
INTERVIEW = {
    "identity": "Operator allocating capital",
    "interests": "unit economics, pricing",
    "triggers": "moats, base rates",
    "ignore": "celebrity",
    "style": "empirical",
    "guidance": "Only falsifiable claims; refuse otherwise",
}
WAIT_LIMIT_SECONDS = 20.0


@pytest.fixture
def store(chorus_home: Path) -> Any:
    from chorus import paths

    jobs = SqliteJobStore(paths.db_path())
    yield jobs
    jobs.close()


def _sample() -> Transcript:
    return FixtureTranscriptProvider().get(EpisodeInput(video_id=SAMPLE))


def _window_count() -> int:
    return len(window_segments(_sample().segments))


def _setup_host(voice: str = "text-only") -> OnboardingConfig:
    agent_setup.set_choice("mode", "host-agent")
    agent_setup.set_choice("brain", "host")
    agent_setup.set_choice("voice", voice)
    agent_setup.set_choice("transcripts", "free")
    agent_setup.soul_save("me", agent_setup.soul_draft("interview", answers=INTERVIEW).markdown)
    agent_setup.set_shows([], [], weekly=False)
    agent_setup.set_choice("updates", "notify")
    return load_config()


def _start(store: Any, episode_format: str = "monologue", episode: str = SAMPLE) -> str:
    return host_mode.start(
        store,
        load_config(),
        [EpisodeInput(video_id=episode)],
        episode_format=episode_format,
        brain_model="test-model-1",
        background=False,
    )


def _scores(*values: float) -> list[dict[str, Any]]:
    return [{"i": i, "score": v, "reason": f"window {i}"} for i, v in enumerate(values)]


def _strong_first_and_last() -> list[dict[str, Any]]:
    n = _window_count()
    return _scores(*[0.9 if i in (0, n - 1) else 0.05 for i in range(n)])


# --- configuration ---------------------------------------------------------


def test_host_brain_needs_host_agent_mode() -> None:
    local = {o.value: o for o in options(Step.brain, OnboardingConfig(mode=Mode.local_agent))}
    hosted = {o.value: o for o in options(Step.brain, OnboardingConfig(mode=Mode.host_agent))}
    assert not local[Brain.host].available
    assert hosted[Brain.host].available


def test_host_brain_needs_no_keys_and_is_ready() -> None:
    _setup_host()
    assert agent_setup.agent_status().ready


def test_start_refuses_a_non_host_brain(store: Any) -> None:
    _setup_host()
    agent_setup.set_choice("brain", "mock")
    with pytest.raises(HostModeError, match="not 'host'"):
        _start(store)


# --- scoring ---------------------------------------------------------------


def test_first_task_is_scoring_with_lens_windows_and_instructions(store: Any) -> None:
    _setup_host()
    task = host_mode.next_task(store, _start(store))
    assert task.kind == "score"
    assert task.rubric_version == RUBRIC_VERSION
    assert task.lens is not None and "Curation Guidance" in task.lens["soul"]
    assert task.episode is not None and task.episode["episode_id"] == SAMPLE
    assert task.episode["window_count"] == _window_count()
    assert task.pending_episodes == [SAMPLE]
    assert "never raise scores" in task.instructions


def test_quotes_come_from_the_transcript_not_the_agent(store: Any) -> None:
    _setup_host()
    job_id = _start(store)
    scores = _strong_first_and_last()
    for item in scores:
        item["quote"] = "A fabricated quote the guest never said."
    outcome = host_mode.submit_scores(store, job_id, SAMPLE, scores)
    assert outcome["episode"]["highlights"] == 2
    task = outcome["next"]
    assert task["kind"] == "script"
    transcript = _sample()
    for h in task["highlights"]:
        assert "fabricated" not in h["quote"]
        assert citation_resolves(transcript, h["at_seconds"], h["quote"])
    assert [h["ref"] for h in task["highlights"]] == [0, 1]


def test_scores_are_clamped_like_the_model_path(store: Any) -> None:
    _setup_host()
    job_id = _start(store)
    n = _window_count()
    outcome = host_mode.submit_scores(store, job_id, SAMPLE, _scores(7.0, *[-3.0] * (n - 1)))
    top = outcome["next"]["highlights"][0]
    assert top["score"] == 1.0


def test_mostly_missing_scores_are_rejected_and_run_stays_open(store: Any) -> None:
    _setup_host()
    job_id = _start(store)
    with pytest.raises(HostModeError, match="scores rejected"):
        host_mode.submit_scores(store, job_id, SAMPLE, [])
    assert host_mode.next_task(store, job_id).kind == "score"


def test_unknown_episode_is_rejected(store: Any) -> None:
    _setup_host()
    job_id = _start(store)
    with pytest.raises(HostModeError, match="not part of run"):
        host_mode.submit_scores(store, job_id, "someone-else", _strong_first_and_last())


def test_all_refused_finishes_without_a_script_task(store: Any) -> None:
    _setup_host()
    job_id = _start(store)
    outcome = host_mode.submit_scores(
        store, job_id, SAMPLE, _scores(*[0.1] * _window_count())
    )
    task = outcome["next"]
    assert task["kind"] == "done"
    episode = task["result"]["episodes"][0]
    assert episode["refused"] and episode["refusal_reason"] == REFUSAL
    job = store.get(job_id)
    assert job.status is JobStatus.done
    assert job.script.monologue == EMPTY_EPISODE_TEXT


# --- script ----------------------------------------------------------------


def _to_script(store: Any, episode_format: str = "monologue") -> str:
    _setup_host()
    job_id = _start(store, episode_format)
    host_mode.submit_scores(store, job_id, SAMPLE, _strong_first_and_last())
    return job_id


def test_script_keeps_grounded_takes_and_drops_the_rest(store: Any) -> None:
    job_id = _to_script(store)
    outcome = host_mode.submit_script(
        store,
        load_config(),
        job_id,
        [
            {"ref": 0, "take_type": "idea", "text": "Unit economics flipped; that is the story."},
            {"ref": 1, "take_type": "pushback", "text": "Licenses are a moat only until..."},
            {"ref": 7, "take_type": "idea", "text": "Cites a highlight that does not exist."},
            {"ref": 0, "take_type": "rant", "text": "Not a take type."},
            {"ref": True, "take_type": "idea", "text": "A boolean is not a ref."},
        ],
    )
    assert len(outcome["dropped"]) == 3
    task = outcome["next"]
    assert task["kind"] == "done"
    assert task["result"]["status"] == "done"
    assert Path(task["result"]["episode_file"]).is_file()
    job = store.get(job_id)
    assert [t.segment_timestamp for t in job.script.takes] == [
        h.segment_timestamp for h in job.digest.highlights
    ]
    assert job.usage.brain == "host"
    assert job.usage.brain_model == "test-model-1"
    assert job.usage.rubric_version == RUBRIC_VERSION
    assert any("dropped ungrounded" in w for w in job.warnings)


def test_script_with_no_grounded_take_is_rejected_and_can_be_retried(store: Any) -> None:
    job_id = _to_script(store)
    with pytest.raises(HostModeError, match="no grounded takes"):
        host_mode.submit_script(
            store, load_config(), job_id, [{"ref": 9, "take_type": "idea", "text": "x"}]
        )
    assert host_mode.next_task(store, job_id).kind == "script"
    host_mode.submit_script(
        store, load_config(), job_id, [{"ref": 0, "take_type": "idea", "text": "Grounded."}]
    )
    assert host_mode.next_task(store, job_id).kind == "done"


def test_dialogue_needs_grounded_turns_and_is_capped(store: Any) -> None:
    job_id = _to_script(store, "dialogue")
    task = host_mode.next_task(store, job_id)
    assert task.episode_format == "dialogue" and task.max_turns
    assert {s["role"] for s in task.speakers or []} == {"host", "cohost"}
    takes = [{"ref": 0, "take_type": "idea", "text": "Beat."}]
    with pytest.raises(HostModeError, match="no grounded turns"):
        host_mode.submit_script(store, load_config(), job_id, takes, [])
    turns = [
        {"ref": i % 2, "speaker": "host" if i % 2 == 0 else "cohost", "text": f"Line {i}."}
        for i in range(task.max_turns + 3)
    ]
    outcome = host_mode.submit_script(store, load_config(), job_id, takes, turns)
    assert any("over the" in d for d in outcome["dropped"])
    job = store.get(job_id)
    assert job.script.format == "dialogue"
    assert len(job.script.turns) == task.max_turns
    assert job.script.monologue.startswith("HOST:")


# --- lifecycle -------------------------------------------------------------


def test_failed_ingest_reports_failure(store: Any) -> None:
    _setup_host()
    job_id = _start(store, episode="no_such_episode")
    task = host_mode.next_task(store, job_id)
    assert task.kind == "failed" and task.error
    assert store.get(job_id).status is JobStatus.failed


def test_background_ingest_then_score(store: Any) -> None:
    _setup_host()
    job_id = host_mode.start(store, load_config(), [EpisodeInput(video_id=SAMPLE)])
    deadline = time.monotonic() + WAIT_LIMIT_SECONDS
    task = host_mode.next_task(store, job_id)
    while task.kind == "wait":
        assert task.wait_seconds
        assert time.monotonic() < deadline, "ingest did not finish"
        time.sleep(0.05)
        task = host_mode.next_task(store, job_id)
    assert task.kind == "score"


def test_run_my_digest_routes_to_the_host_brain(store: Any) -> None:
    _setup_host()
    started = submit_configured_digest(store, [SAMPLE], agent_model="claude-x")
    assert started["brain"] == "host"
    assert store.get(started["job_id"]).usage.brain_model == "claude-x"
    # Let the background ingest finish inside this test's isolated home.
    deadline = time.monotonic() + WAIT_LIMIT_SECONDS
    while host_mode.next_task(store, started["job_id"]).kind == "wait":
        assert time.monotonic() < deadline, "ingest did not finish"
        time.sleep(0.05)


def test_smoke_test_completes_when_the_agent_finishes_the_run(store: Any) -> None:
    _setup_host()
    started = agent_setup.smoke_test(run=True, background=False, agent_model="grok-test")
    job_id = started["host_job_id"]
    assert started["next"]["kind"] == "score"
    assert store.get(job_id).usage.brain_model == "grok-test"
    assert Step.smoke_test not in load_config().completed

    host_mode.submit_scores(store, job_id, SAMPLE, _strong_first_and_last())
    host_mode.submit_script(
        store, load_config(), job_id, [{"ref": 0, "take_type": "idea", "text": "Grounded."}]
    )
    status = agent_setup.agent_status()
    assert status.next is None
    assert Step.smoke_test in load_config().completed


# --- CLI transport ---------------------------------------------------------


def test_cli_host_run(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    _setup_host()

    def call(*argv: str) -> dict[str, Any]:
        assert cli_main(["setup", *argv]) == EXIT_OK
        return json.loads(capsys.readouterr().out)

    task = call("host-start", "--episode", SAMPLE, "--agent-model", "codex-test")
    assert task["kind"] == "score"
    job_id = task["job_id"]
    scores = tmp_path / "scores.json"
    scores.write_text(json.dumps({"scores": _strong_first_and_last()}), encoding="utf-8")
    assert call("host-scores", job_id, SAMPLE, "--file", str(scores))["next"]["kind"] == "script"
    script = tmp_path / "script.json"
    script.write_text(
        json.dumps({"takes": [{"ref": 0, "take_type": "idea", "text": "Grounded."}]}),
        encoding="utf-8",
    )
    assert call("host-script", job_id, "--file", str(script))["next"]["kind"] == "done"
    assert call("host-next", job_id)["result"]["status"] == "done"
