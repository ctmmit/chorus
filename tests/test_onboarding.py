"""Onboarding: soul validation, the step state machine, config persistence,
the terminal wizard driven by scripted answers, and the local runner gate.

Every test points CHORUS_HOME at a temp directory, so nothing touches the
real ~/.chorus, and no live provider is ever called (mock brain, text voice,
the bundled synthetic transcript).
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from chorus import paths
from chorus.brains import BrainConfigError, build_thinking, restrict_transcript_env
from chorus.cli import EXIT_NOT_READY, EXIT_OK
from chorus.cli import main as cli_main
from chorus.local_run import run_digest
from chorus.models import EpisodeInput, JobStatus
from chorus.onboarding import (
    CONFIG_SCHEMA_VERSION,
    Brain,
    Mode,
    OnboardingConfig,
    OnboardingError,
    Step,
    Transcripts,
    UpdatePolicy,
    Voice,
    apply,
    dump_config,
    load_config,
    mark_done,
    options,
    reset,
    save_config,
    set_env_value,
    status,
)
from chorus.paths import _copy_legacy as REAL_COPY_LEGACY  # captured before the fixture stubs it
from chorus.soul import (
    SoulError,
    SoulSection,
    load_preset,
    save_soul,
    soul_path,
    validate_soul,
)
from chorus.wizard import Wizard, WizardIO

GOOD_SOUL = """# Soul (test)

## Identity & Role
An operator who allocates capital.

## Core Interests
- unit economics

## Attention Triggers
- moats, pricing power

## Anti-interests
- celebrity gossip

## Taste & Sensibility
Empirical.

## Curation Guidance
Surface only falsifiable claims with a mechanism; refuse rather than pad.
"""


@pytest.fixture(autouse=True)
def chorus_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    home = tmp_path / "chorus-home"
    monkeypatch.setenv(paths.CHORUS_HOME_ENV, str(home))
    for env in (
        "ANTHROPIC_API_KEY",
        "ELEVENLABS_API_KEY",
        "ASSEMBLYAI_API_KEY",
        "DEEPGRAM_API_KEY",
        "TRANSCRIPT_API_KEY",
    ):
        monkeypatch.delenv(env, raising=False)
    # Never read or copy the checkout's real .env.local / chorus.db in tests.
    monkeypatch.setattr(paths, "_copy_legacy", lambda source, target: None)
    monkeypatch.setattr("chorus.cli.load_env", lambda: None)
    home.mkdir()
    for sub in ("souls", "artifacts", "backups", "extensions"):
        (home / sub).mkdir()
    yield home


def _ready_config(**overrides: object) -> OnboardingConfig:
    save_soul("me", GOOD_SOUL)
    config = OnboardingConfig(
        mode=Mode.local_agent,
        brain=Brain.mock,
        voice=Voice.text_only,
        transcripts=Transcripts.free,
        soul="me",
        updates=UpdatePolicy.notify,
    )
    config = config.model_copy(update=overrides)
    for step in Step:
        config = mark_done(config, step)
    return config


# --- soul ------------------------------------------------------------------


def test_complete_soul_is_valid() -> None:
    check = validate_soul(GOOD_SOUL)
    assert check.valid
    assert check.missing == [] and check.empty == [] and check.warnings == []


@pytest.mark.parametrize("preset", ["investor", "popculture"])
def test_bundled_presets_validate(preset: str) -> None:
    # Presets use short headings ("Identity", "Ignore", "Communication style").
    check = validate_soul(load_preset(preset))
    assert check.valid, check
    assert check.warnings == [f"no '{SoulSection.core_interests.value}' section"]


def test_soul_missing_required_section_is_invalid() -> None:
    without_guidance = GOOD_SOUL.split("## Curation Guidance")[0]
    check = validate_soul(without_guidance)
    assert not check.valid
    assert check.missing == [SoulSection.guidance.value]


def test_soul_with_placeholder_triggers_is_invalid() -> None:
    hollow = GOOD_SOUL.replace("- moats, pricing power", "- (none inferred)")
    check = validate_soul(hollow)
    assert not check.valid
    assert check.empty == [SoulSection.attention_triggers.value]


def test_anti_interests_may_be_none() -> None:
    assert validate_soul(GOOD_SOUL.replace("- celebrity gossip", "- (none inferred)")).valid


def test_save_soul_rejects_invalid_and_backs_up_on_change(chorus_home: Path) -> None:
    with pytest.raises(SoulError, match="missing sections"):
        save_soul("me", "# nothing here")
    save_soul("me", GOOD_SOUL)
    save_soul("me", GOOD_SOUL.replace("Empirical.", "Contrarian."))
    assert "Contrarian." in soul_path("me").read_text(encoding="utf-8")
    backups = list((chorus_home / "backups").glob("soul-me-*.md"))
    assert len(backups) == 1
    assert "Empirical." in backups[0].read_text(encoding="utf-8")


def test_soul_names_are_restricted() -> None:
    with pytest.raises(SoulError):
        soul_path("../escape")


def test_legacy_state_is_copied_once(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.env"
    legacy.write_text("A=1", encoding="utf-8")
    target = tmp_path / "home.env"
    REAL_COPY_LEGACY(legacy, target)
    legacy.write_text("A=2", encoding="utf-8")
    REAL_COPY_LEGACY(legacy, target)  # an existing target is never overwritten
    assert target.read_text(encoding="utf-8") == "A=1"
    assert legacy.exists()  # copied, not moved


# --- state machine ---------------------------------------------------------


def test_host_brain_and_plugin_voice_are_not_offered_yet() -> None:
    config = OnboardingConfig(mode=Mode.host_agent)
    brain = {o.value: o for o in options(Step.brain, config)}
    voice = {o.value: o for o in options(Step.voice, config)}
    assert not brain[Brain.host].available
    assert not voice[Voice.host_plugin].available
    with pytest.raises(OnboardingError, match="unavailable"):
        apply(Step.brain, Brain.host, config)


def test_apply_rejects_unknown_value() -> None:
    with pytest.raises(OnboardingError, match="not an option"):
        apply(Step.voice, "carrier-pigeon", OnboardingConfig())


def test_apply_records_choice_and_marks_step_done() -> None:
    config = apply(Step.brain, Brain.anthropic, OnboardingConfig())
    assert config.brain is Brain.anthropic
    assert Step.brain in config.completed


def test_not_ready_without_soul() -> None:
    config = _ready_config()
    config = reset(config, Step.soul)
    current = status(config, environ={})
    assert not current.ready
    assert current.next_step is Step.soul


def test_missing_soul_file_blocks_even_when_marked_done(chorus_home: Path) -> None:
    config = _ready_config()
    soul_path("me").unlink()
    soul_row = next(r for r in status(config, environ={}).steps if r.step is Step.soul)
    assert not soul_row.done
    assert soul_row.blocker and "no soul named" in soul_row.blocker


def test_chosen_paid_provider_without_key_blocks() -> None:
    config = _ready_config(brain=Brain.anthropic, voice=Voice.elevenlabs_key)
    current = status(config, environ={"ANTHROPIC_API_KEY": "sk-test"})
    assert not current.ready
    assert current.missing_keys == ["ELEVENLABS_API_KEY"]
    assert status(
        config, environ={"ANTHROPIC_API_KEY": "sk-test", "ELEVENLABS_API_KEY": "el-test"}
    ).ready


def test_mock_setup_is_ready_with_no_keys() -> None:
    assert status(_ready_config(), environ={}).ready


# --- persistence -----------------------------------------------------------


def test_config_round_trips_through_toml(tmp_path: Path) -> None:
    config = _ready_config(feeds=['https://example.com/feed?q="x"&y=\\z'], shows=["Ünïcode Show"])
    target = save_config(config, tmp_path / "config.toml")
    assert load_config(target) == config


def test_newer_config_schema_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(dump_config(OnboardingConfig()), encoding="utf-8")
    text = target.read_text(encoding="utf-8").replace(
        f"schema_version = {CONFIG_SCHEMA_VERSION}", f"schema_version = {CONFIG_SCHEMA_VERSION + 1}"
    )
    target.write_text(text, encoding="utf-8")
    with pytest.raises(OnboardingError, match="newer than this Chorus"):
        load_config(target)


def test_set_env_value_replaces_in_place(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# keep me\nA=1\nB=2\nA=3\n", encoding="utf-8")
    set_env_value(env, "A", "new")
    set_env_value(env, "C", "added")
    assert env.read_text(encoding="utf-8") == "# keep me\nA=new\nB=2\nC=added\n"
    with pytest.raises(OnboardingError):
        set_env_value(env, "D", "two\nlines")


# --- brains ----------------------------------------------------------------


def test_chosen_brain_without_key_is_an_error_not_a_mock() -> None:
    with pytest.raises(BrainConfigError, match="ANTHROPIC_API_KEY"):
        build_thinking(OnboardingConfig(brain=Brain.anthropic))


def test_unchosen_transcript_keys_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg")
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "aai")
    removed = restrict_transcript_env(OnboardingConfig(transcripts=Transcripts.assemblyai))
    assert removed == ["DEEPGRAM_API_KEY"]
    import os

    assert os.environ.get("ASSEMBLYAI_API_KEY") == "aai"


# --- wizard ----------------------------------------------------------------


class ScriptedIO:
    def __init__(self, answers: list[str], secrets: list[str] | None = None) -> None:
        self.answers = list(answers)
        self.secrets = list(secrets or [])
        self.output: list[str] = []

    def ask(self, prompt: str) -> str:
        self.output.append(prompt)
        if not self.answers:
            raise AssertionError(f"wizard asked an unscripted question: {prompt!r}")
        return self.answers.pop(0)

    def secret(self, prompt: str) -> str:
        self.output.append(prompt)
        return self.secrets.pop(0)

    def say(self, text: str) -> None:
        self.output.append(text)

    def io(self) -> WizardIO:
        return WizardIO(ask=self.ask, secret=self.secret, say=self.say)


FULL_MOCK_RUN = [
    "1",  # mode: local agent
    "1",  # brain: "agent's own model" is unavailable -> re-prompted
    "3",  # brain: demo
    "3",  # voice: text only
    "1",  # transcripts: free
    "",  # soul name: default "me"
    "4",  # soul source: preset
    "investor",
    "y",  # use this soul
    "",  # shows: none
    "",  # feeds: none
    "n",  # weekly
    "1",  # updates: notify
    "",  # smoke test: default yes (nothing billed)
]


def test_wizard_end_to_end_with_mock_providers(chorus_home: Path) -> None:
    scripted = ScriptedIO(FULL_MOCK_RUN)
    config = Wizard(io=scripted.io()).run(OnboardingConfig())

    assert status(config, environ={}).ready
    assert config.soul == "me" and config.brain is Brain.mock
    assert soul_path("me").is_file()
    assert any("unavailable" in line for line in scripted.output)
    assert any("Status: done" in line for line in scripted.output)
    assert load_config() == config  # saved as it went
    assert scripted.answers == []


def test_wizard_resumes_without_asking_again() -> None:
    config = Wizard(io=ScriptedIO(FULL_MOCK_RUN).io()).run(OnboardingConfig())
    again = ScriptedIO([])
    assert Wizard(io=again.io()).run(config) == config


def test_wizard_reset_reopens_only_that_step() -> None:
    config = Wizard(io=ScriptedIO(FULL_MOCK_RUN).io()).run(OnboardingConfig())
    rerun = ScriptedIO(["2"])  # updates: automatic
    updated = Wizard(io=rerun.io()).run(config, [Step.updates])
    assert updated.updates is UpdatePolicy.auto
    assert rerun.answers == []


def test_wizard_saves_skipped_key_step_as_incomplete(chorus_home: Path) -> None:
    config = OnboardingConfig(brain=Brain.anthropic)
    wizard = Wizard(io=ScriptedIO([], secrets=[""]).io())
    assert Step.keys not in wizard._keys(config).completed
    saved = Wizard(io=ScriptedIO([], secrets=["sk-test"]).io())._keys(config)
    assert Step.keys in saved.completed
    assert "ANTHROPIC_API_KEY=sk-test" in (chorus_home / ".env").read_text(encoding="utf-8")


def test_interview_soul_is_built_and_validated() -> None:
    answers = [
        "",  # soul name: default
        "1",  # interview
        "Operator allocating capital",
        "unit economics, pricing",
        "moats, base rates",
        "celebrity",
        "empirical",
        "Only falsifiable claims; refuse otherwise",
        "y",
    ]
    config = Wizard(io=ScriptedIO(answers).io())._soul(OnboardingConfig(brain=Brain.mock))
    assert config.soul == "me"
    assert validate_soul(soul_path("me").read_text(encoding="utf-8")).valid


# --- local runner + CLI ----------------------------------------------------


def test_run_refuses_until_onboarded() -> None:
    with pytest.raises(OnboardingError, match="incomplete"):
        run_digest(OnboardingConfig(), [EpisodeInput(video_id="sample_public")])


def test_run_digest_on_sample_transcript() -> None:
    job = run_digest(_ready_config(), [EpisodeInput(video_id="sample_public")])
    assert job.status is JobStatus.done
    assert job.digest is not None
    assert (paths.artifacts_dir() / job.audio_url.rsplit("/", 1)[-1]).is_file()  # type: ignore[union-attr]


def test_cli_status_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main(["status"]) == EXIT_NOT_READY
    save_config(_ready_config())
    assert cli_main(["status"]) == EXIT_OK
    assert "Ready." in capsys.readouterr().out
