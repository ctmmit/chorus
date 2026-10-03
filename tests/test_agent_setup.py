"""Agent-driven onboarding: the transport-neutral layer (`chorus.agent_setup`),
its MCP transport (local stdio only), and its JSON CLI transport.

A host agent (Claude Code, Cowork, Codex, Grok Build, ...) walks the same
steps as the terminal wizard. Offline throughout: mock brain, text voice,
the bundled synthetic transcript, an isolated CHORUS_HOME.
"""
from __future__ import annotations

import io
import json
import time
from pathlib import Path

import anyio
import pytest

from chorus import agent_setup
from chorus.audio import MockAudioRenderer
from chorus.cli import EXIT_NOT_READY, EXIT_OK
from chorus.cli import main as cli_main
from chorus.jobs import SqliteJobStore
from chorus.llm import MockLLMClient
from chorus.mcp_server import create_mcp_server
from chorus.mcp_setup import submit_configured_digest
from chorus.models import JobStatus
from chorus.onboarding import OnboardingError, Step
from chorus.pipeline import Deps
from chorus.script import MockScriptComposer
from chorus.soul import validate_soul
from chorus.transcripts import FixtureTranscriptProvider

pytestmark = pytest.mark.usefixtures("chorus_home")

INTERVIEW = {
    "identity": "Operator allocating capital",
    "interests": "unit economics, pricing",
    "triggers": "moats, base rates",
    "ignore": "celebrity",
    "style": "empirical",
    "guidance": "Only falsifiable claims; refuse otherwise",
}
SETUP_TOOLS = {
    "onboarding_status",
    "onboarding_options",
    "onboarding_set",
    "onboarding_set_key",
    "onboarding_voices",
    "onboarding_set_voice",
    "onboarding_soul_draft",
    "onboarding_soul_save",
    "onboarding_soul_show",
    "onboarding_set_shows",
    "onboarding_smoke_test",
    "run_my_digest",
}
JOB_WAIT_SECONDS = 20.0


def _walk_to_ready() -> None:
    """What a host agent does after asking its principal each question."""
    agent_setup.set_choice("mode", "host-agent")
    agent_setup.set_choice("brain", "mock")
    agent_setup.set_choice("voice", "text-only")
    agent_setup.set_choice("transcripts", "free")
    draft = agent_setup.soul_draft("interview", answers=INTERVIEW)
    agent_setup.soul_save("me", draft.markdown)
    agent_setup.set_shows([], ["https://example.com/feed.xml"], weekly=True)
    agent_setup.set_choice("updates", "notify")


def _tool_names(local: bool, tmp_path: Path) -> set[str]:
    store = SqliteJobStore(tmp_path / "jobs.db")
    deps = Deps(
        FixtureTranscriptProvider(), MockLLMClient(), MockScriptComposer(), MockAudioRenderer()
    )
    server, _ = create_mcp_server(store, deps, local=local)
    tools = anyio.run(server.list_tools)
    store.close()
    return {t.name for t in tools}


# --- the walk --------------------------------------------------------------


def test_fresh_install_starts_at_mode_with_instructions() -> None:
    status = agent_setup.agent_status()
    assert not status.ready
    assert status.next is not None and status.next.step is Step.mode
    assert status.next.kind == "choice"
    assert status.next.ask and status.next.agent_notes
    assert {o.value for o in status.next.options} == {"local-agent", "host-agent"}


def test_agent_walk_reaches_ready_and_smoke_test_runs() -> None:
    _walk_to_ready()
    status = agent_setup.agent_status()
    # Keys completed itself (mock brain + text voice need none); only the
    # smoke test is left, and it is not a precondition for running.
    assert status.ready
    assert status.next is not None and status.next.step is Step.smoke_test
    assert status.next.data["billed"] is False

    outcome = agent_setup.smoke_test(run=True)
    assert outcome["result"]["status"] == "done"
    assert outcome["result"]["episodes"][0]["episode_id"] == "sample_public"
    assert outcome["status"]["next"] is None


def test_soul_step_is_never_skippable() -> None:
    agent_setup.set_choice("mode", "host-agent")
    agent_setup.set_choice("brain", "mock")
    agent_setup.set_choice("voice", "text-only")
    agent_setup.set_choice("transcripts", "free")
    agent_setup.set_shows([], [], weekly=False)
    agent_setup.set_choice("updates", "off")
    status = agent_setup.agent_status()
    assert not status.ready
    assert status.next is not None and status.next.step is Step.soul
    assert [q["key"] for q in status.next.data["questions"]] == list(INTERVIEW)
    with pytest.raises(OnboardingError, match="finish these steps first: soul"):
        agent_setup.smoke_test(run=True)


def test_soul_draft_reports_problems_instead_of_saving() -> None:
    draft = agent_setup.soul_draft("write", markdown="# Soul\n\n## Identity & Role\nMe.\n")
    assert not draft.check.valid
    assert "Curation Guidance" in draft.check.missing
    assert any("Not usable yet" in n for n in draft.agent_notes)
    with pytest.raises(ValueError, match="missing sections"):
        agent_setup.soul_save("me", draft.markdown)


def test_agent_written_soul_is_accepted() -> None:
    agent_written = agent_setup.soul_draft("preset", preset="investor").markdown
    agent_setup.soul_save("investor-me", agent_written)
    shown = agent_setup.soul_show()
    assert shown["name"] == "investor-me" and validate_soul(shown["markdown"]).valid


def test_keys_prompt_warns_about_transcripts_and_offers_the_file() -> None:
    agent_setup.set_choice("mode", "local-agent")
    agent_setup.set_choice("brain", "anthropic")
    agent_setup.set_choice("voice", "text-only")
    agent_setup.set_choice("transcripts", "free")
    status = agent_setup.agent_status()
    assert status.next is not None and status.next.step is Step.keys
    assert [k["env"] for k in status.next.data["keys"]] == ["ANTHROPIC_API_KEY"]
    assert any("transcript" in n for n in status.next.agent_notes)

    after = agent_setup.set_key("ANTHROPIC_API_KEY", "sk-test\n")
    assert Step.keys in [r.step for r in after.status.steps if r.done]


def test_unknown_key_names_are_refused(chorus_home: Path) -> None:
    with pytest.raises(OnboardingError, match="not a key Chorus stores"):
        agent_setup.set_key("PATH", "/evil")
    assert not (chorus_home / ".env").exists()


def test_shows_must_be_in_catalog_or_be_feed_urls() -> None:
    with pytest.raises(OnboardingError, match="not in the catalog"):
        agent_setup.set_shows(["Not A Real Show"], [], weekly=False)
    with pytest.raises(OnboardingError, match="http"):
        agent_setup.set_shows([], ["ftp://feed"], weekly=False)


# --- MCP transport ---------------------------------------------------------


def test_hosted_mcp_never_exposes_setup_tools(tmp_path: Path) -> None:
    assert not SETUP_TOOLS & _tool_names(local=False, tmp_path=tmp_path)


def test_local_mcp_exposes_setup_tools(tmp_path: Path) -> None:
    assert _tool_names(local=True, tmp_path=tmp_path) >= SETUP_TOOLS


def test_run_my_digest_refuses_until_ready(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    with pytest.raises(OnboardingError, match="setup is incomplete"):
        submit_configured_digest(store, ["sample_public"])
    store.close()


def test_run_my_digest_runs_in_background(tmp_path: Path) -> None:
    _walk_to_ready()
    store = SqliteJobStore(tmp_path / "jobs.db")
    job_id = submit_configured_digest(store, ["sample_public"])["job_id"]
    deadline = time.monotonic() + JOB_WAIT_SECONDS
    job = store.get(job_id)
    while job is not None and job.status not in (JobStatus.done, JobStatus.failed):
        assert time.monotonic() < deadline, "digest did not finish"
        time.sleep(0.05)
        job = store.get(job_id)
    assert job is not None and job.status is JobStatus.done
    store.close()


# --- JSON CLI transport ----------------------------------------------------


def test_cli_setup_walk_prints_json(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def call(*argv: str) -> dict[str, object]:
        assert cli_main(["setup", *argv]) == EXIT_OK
        return json.loads(capsys.readouterr().out)

    assert call("status")["ready"] is False
    call("set", "mode", "host-agent")
    call("set", "brain", "mock")
    call("set", "voice", "text-only")
    call("set", "transcripts", "free")
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps(INTERVIEW), encoding="utf-8")
    draft = call("soul-draft", "--source", "interview", "--answers", str(answers))
    soul = tmp_path / "soul.md"
    soul.write_text(str(draft["markdown"]), encoding="utf-8")
    call("soul-save", "me", "--file", str(soul))
    call("shows", "--feed", "https://example.com/feed.xml")
    status = call("set", "updates", "notify")
    assert status["ready"] is True

    smoke = call("smoke")
    assert smoke["result"]["status"] == "done"  # type: ignore[index]


def test_cli_setup_key_reads_stdin_not_argv(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, chorus_home: Path
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("sk-from-stdin\n"))
    assert cli_main(["setup", "key", "ANTHROPIC_API_KEY"]) == EXIT_OK
    capsys.readouterr()
    assert "ANTHROPIC_API_KEY=sk-from-stdin" in (chorus_home / ".env").read_text(encoding="utf-8")


def test_cli_setup_errors_are_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main(["setup", "set", "brain", "telepathy"]) == EXIT_NOT_READY
    assert "not an option" in json.loads(capsys.readouterr().out)["error"]
