"""Choosing the ElevenLabs voices during onboarding.

The voices step follows the voice choice: with `elevenlabs-key` Chorus lists
the voices on the principal's account; with `host-plugin` the agent lists
them with its own tool. Either way the chosen ids reach every renderer.
The step is optional (skipping keeps the default voices) and never blocks a
run. Offline throughout: the ElevenLabs API is an httpx.MockTransport or a
fake catalog.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from chorus import agent_setup
from chorus.audio import (
    DEFAULT_COHOST_VOICE_ID,
    DEFAULT_VOICE_ID,
    ElevenLabsDialogueRenderer,
    ElevenLabsRenderer,
    ProfileAwareRenderer,
)
from chorus.brains import build_voice
from chorus.cli import EXIT_OK
from chorus.cli import main as cli_main
from chorus.onboarding import (
    Mode,
    OnboardingConfig,
    OnboardingError,
    Step,
    Voice,
    reset,
    status,
    voice_overrides,
)
from chorus.voices import (
    VOICES_URL,
    ElevenLabsVoiceCatalog,
    InvalidVoiceId,
    VoiceInfo,
    VoiceListError,
    check_voice_id,
)
from chorus.wizard import Wizard, WizardIO

pytestmark = pytest.mark.usefixtures("chorus_home")

FAKE_KEY = "test-elevenlabs-key"
ARIA = VoiceInfo(
    voice_id="9BWtsMINqrJLrRacOk9x",
    name="Aria",
    category="premade",
    labels={"accent": "american", "gender": "female", "use_case": "narration"},
)
GEORGE = VoiceInfo(voice_id="JBFqnCBsd6RMkjVDRZzb", name="George", category="premade")
MY_CLONE = VoiceInfo(voice_id="abcdefghij0123456789", name="My clone", category="cloned")
ACCOUNT_VOICES = [ARIA, GEORGE, MY_CLONE]


class FakeCatalog:
    def __init__(self, voices: list[VoiceInfo] | None = None, error: str | None = None) -> None:
        self.voices = voices if voices is not None else ACCOUNT_VOICES
        self.error = error

    def list_voices(self) -> list[VoiceInfo]:
        if self.error:
            raise VoiceListError(self.error)
        return list(self.voices)


def _voice_json(voice: VoiceInfo) -> dict[str, object]:
    return voice.model_dump()


# --- the ElevenLabs catalog ------------------------------------------------


def test_catalog_pages_through_voices_with_the_key_and_sorts_by_name() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.params.get("next_page_token") == "p2":
            return httpx.Response(200, json={"voices": [_voice_json(ARIA)], "has_more": False})
        return httpx.Response(
            200,
            json={
                "voices": [_voice_json(MY_CLONE), _voice_json(GEORGE)],
                "has_more": True,
                "next_page_token": "p2",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        voices = ElevenLabsVoiceCatalog(FAKE_KEY, client=client).list_voices()

    assert [v.name for v in voices] == ["Aria", "George", "My clone"]
    assert voices[0].labels["accent"] == "american"
    assert len(seen) == 2
    assert all(str(r.url).startswith(VOICES_URL) for r in seen)
    assert all(r.headers["xi-api-key"] == FAKE_KEY for r in seen)


def test_catalog_explains_a_key_that_cannot_list_voices() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(401, json={"detail": "nope"}))
    with httpx.Client(transport=transport) as client, pytest.raises(VoiceListError) as err:
        ElevenLabsVoiceCatalog(FAKE_KEY, client=client).list_voices()
    assert "401" in str(err.value) and "voice id" in str(err.value)


def test_catalog_reports_network_failures_as_list_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client, pytest.raises(VoiceListError):
        ElevenLabsVoiceCatalog(FAKE_KEY, client=client).list_voices()


def test_voice_ids_are_checked_before_they_are_stored() -> None:
    assert check_voice_id(f"  {ARIA.voice_id} ") == ARIA.voice_id
    for bad in ("", "Rachel", "has spaces in it here", "x" * 60, "abc/def/ghi/jkl/mno"):
        with pytest.raises(InvalidVoiceId):
            check_voice_id(bad)


# --- the onboarding step ---------------------------------------------------


def test_voices_step_applies_only_to_voices_someone_renders() -> None:
    rows = {r.step: r for r in status(OnboardingConfig(voice=Voice.text_only), environ={}).steps}
    assert rows[Step.voices].done
    for voice in (Voice.elevenlabs_key, Voice.host_plugin):
        rows = {r.step: r for r in status(OnboardingConfig(voice=voice), environ={}).steps}
        assert not rows[Step.voices].done


def test_unchosen_voices_never_block_a_run(chorus_home: Path) -> None:
    agent_setup.set_choice("mode", "local-agent")
    agent_setup.set_choice("brain", "mock")
    agent_setup.set_choice("voice", "elevenlabs-key")
    agent_setup.set_choice("transcripts", "free")
    agent_setup.set_key("ELEVENLABS_API_KEY", FAKE_KEY)
    draft = agent_setup.soul_draft("preset", preset="investor")
    agent_setup.soul_save("me", draft.markdown)
    agent_setup.set_shows([], [], weekly=False)
    agent_setup.set_choice("updates", "notify")
    agent_setup.smoke_test(run=False)

    current = agent_setup.agent_status()
    assert current.ready
    assert current.next is not None and current.next.step is Step.voices


def test_resetting_voices_clears_the_choice() -> None:
    chosen = OnboardingConfig(
        voice=Voice.elevenlabs_key,
        host_voice_id=ARIA.voice_id,
        host_voice_name=ARIA.name,
        cohost_voice_id=GEORGE.voice_id,
        cohost_voice_name=GEORGE.name,
        completed=[Step.voices],
    )
    cleared = reset(chosen, Step.voices)
    assert cleared.host_voice_id is None and cleared.cohost_voice_id is None
    assert cleared.host_voice_name is None and Step.voices not in cleared.completed


def test_chosen_voices_override_the_environment() -> None:
    environ = {"ELEVENLABS_VOICE_ID": "envhostvoice00000000", "ELEVENLABS_COHOST_VOICE_ID": "c" * 20}
    config = OnboardingConfig(host_voice_id=ARIA.voice_id)
    assert voice_overrides(config, environ) == {"host": ARIA.voice_id, "cohost": "c" * 20}
    assert voice_overrides(OnboardingConfig(), {}) == {}


# --- agents: MCP and JSON CLI ---------------------------------------------


@pytest.fixture
def elevenlabs_chosen(chorus_home: Path, monkeypatch: pytest.MonkeyPatch) -> FakeCatalog:
    catalog = FakeCatalog()
    monkeypatch.setattr(agent_setup, "make_voice_catalog", lambda key: catalog)
    agent_setup.set_choice("voice", "elevenlabs-key")
    agent_setup.set_key("ELEVENLABS_API_KEY", FAKE_KEY)
    return catalog


def test_agent_lists_the_account_voices(elevenlabs_chosen: FakeCatalog) -> None:
    listing = agent_setup.list_voices()
    assert listing["source"] == "chorus"
    assert [v["name"] for v in listing["voices"]] == ["Aria", "George", "My clone"]
    assert listing["defaults"]["host"]["voice_id"] == DEFAULT_VOICE_ID


def test_agent_gets_a_paste_fallback_when_listing_fails(elevenlabs_chosen: FakeCatalog) -> None:
    elevenlabs_chosen.error = "the ElevenLabs key cannot list voices (HTTP 401)"
    listing = agent_setup.list_voices()
    assert listing["voices"] == [] and "401" in listing["error"]
    assert any("voice id" in note for note in listing["agent_notes"])


def test_agent_voice_lists_voices_with_its_own_tool(chorus_home: Path) -> None:
    agent_setup.set_choice("mode", "host-agent")
    agent_setup.set_choice("voice", "host-plugin")
    listing = agent_setup.list_voices()
    assert listing["source"] == "agent" and listing["voices"] == []
    assert any("your own" in note for note in listing["agent_notes"])


def test_listing_voices_without_a_voice_to_render_is_an_error(chorus_home: Path) -> None:
    agent_setup.set_choice("voice", "text-only")
    with pytest.raises(OnboardingError):
        agent_setup.list_voices()


def test_agent_sets_host_and_cohost_voices(elevenlabs_chosen: FakeCatalog) -> None:
    result = agent_setup.set_voice(ARIA.voice_id, GEORGE.voice_id)
    config = agent_setup._load()
    assert (config.host_voice_id, config.cohost_voice_id) == (ARIA.voice_id, GEORGE.voice_id)
    # Names come from the account listing when the agent passes none.
    assert (config.host_voice_name, config.cohost_voice_name) == ("Aria", "George")
    rows = {r.step: r for r in result.status.steps}
    assert rows[Step.voices].done


def test_agent_skip_keeps_the_defaults(elevenlabs_chosen: FakeCatalog) -> None:
    agent_setup.set_voice(None, None)
    config = agent_setup._load()
    assert config.host_voice_id is None and Step.voices in config.completed


def test_agent_voice_choice_is_validated(elevenlabs_chosen: FakeCatalog) -> None:
    with pytest.raises(OnboardingError):
        agent_setup.set_voice("Rachel please", None)
    with pytest.raises(OnboardingError):
        agent_setup.set_voice(ARIA.voice_id, ARIA.voice_id)


def test_voices_prompt_points_the_agent_at_the_listing(elevenlabs_chosen: FakeCatalog) -> None:
    prompt = agent_setup.step_options("voices")
    assert prompt.kind == "voices"
    assert any("onboarding_voices" in note for note in prompt.agent_notes)


def test_json_cli_lists_and_sets_voices(
    elevenlabs_chosen: FakeCatalog, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_main(["setup", "voices"]) == EXIT_OK
    assert [v["name"] for v in json.loads(capsys.readouterr().out)["voices"]][0] == "Aria"
    assert cli_main(["setup", "voice", "--host", GEORGE.voice_id]) == EXIT_OK
    capsys.readouterr()
    assert agent_setup._load().host_voice_id == GEORGE.voice_id


# --- the terminal wizard ---------------------------------------------------


class Scripted:
    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.output: list[str] = []

    def ask(self, prompt: str) -> str:
        self.output.append(prompt)
        if not self.answers:
            raise AssertionError(f"wizard asked an unscripted question: {prompt!r}")
        return self.answers.pop(0)

    def io(self) -> WizardIO:
        return WizardIO(ask=self.ask, secret=self.ask, say=self.output.append)


def _eleven(**update: object) -> OnboardingConfig:
    return OnboardingConfig(mode=Mode.local_agent, voice=Voice.elevenlabs_key).model_copy(
        update=update
    )


def test_wizard_picks_host_and_cohost_from_the_account(
    chorus_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", FAKE_KEY)
    scripted = Scripted(["1", "y", "2"])  # Aria, add a co-host, George
    wizard = Wizard(io=scripted.io(), voice_catalog=lambda key: FakeCatalog())

    config = wizard._voices(_eleven())

    assert (config.host_voice_id, config.cohost_voice_id) == (ARIA.voice_id, GEORGE.voice_id)
    assert Step.voices in config.completed
    assert any("Aria" in line and "american" in line for line in scripted.output)


def test_wizard_enter_keeps_the_default_voices(
    chorus_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", FAKE_KEY)
    wizard = Wizard(io=Scripted(["", "n"]).io(), voice_catalog=lambda key: FakeCatalog())
    config = wizard._voices(_eleven())
    assert config.host_voice_id is None and Step.voices in config.completed


def test_wizard_falls_back_to_a_pasted_voice_id(
    chorus_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", FAKE_KEY)
    failing = FakeCatalog(error="the ElevenLabs key cannot list voices (HTTP 401)")
    scripted = Scripted(["not a voice id", MY_CLONE.voice_id, ""])  # retry, host, no co-host
    wizard = Wizard(io=scripted.io(), voice_catalog=lambda key: failing)

    config = wizard._voices(_eleven())

    assert config.host_voice_id == MY_CLONE.voice_id and config.cohost_voice_id is None
    assert any("401" in line for line in scripted.output)


def test_wizard_waits_for_the_key_before_listing(chorus_home: Path) -> None:
    wizard = Wizard(io=Scripted([]).io(), voice_catalog=lambda key: FakeCatalog())
    config = wizard._voices(_eleven())
    assert Step.voices not in config.completed


def test_wizard_says_nothing_to_pick_for_text_only(chorus_home: Path) -> None:
    config = OnboardingConfig(voice=Voice.text_only)
    assert Step.voices in Wizard(io=Scripted([]).io())._voices(config).completed


# --- the chosen voices reach the renderers ---------------------------------


def test_chorus_renders_with_the_chosen_voices(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", FAKE_KEY)
    renderer = build_voice(_eleven(host_voice_id=ARIA.voice_id, cohost_voice_id=GEORGE.voice_id))
    assert isinstance(renderer, ProfileAwareRenderer)
    mono, dialogue = renderer.monologue_renderer, renderer.dialogue_renderer
    assert isinstance(mono, ElevenLabsRenderer) and mono.voice_id == ARIA.voice_id
    assert isinstance(dialogue, ElevenLabsDialogueRenderer)
    assert (dialogue.host_voice_id, dialogue.cohost_voice_id) == (ARIA.voice_id, GEORGE.voice_id)


def test_unchosen_voices_fall_back_to_the_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", FAKE_KEY)
    monkeypatch.delenv("ELEVENLABS_VOICE_ID", raising=False)
    monkeypatch.delenv("ELEVENLABS_COHOST_VOICE_ID", raising=False)
    renderer = build_voice(_eleven())
    assert isinstance(renderer, ProfileAwareRenderer)
    dialogue = renderer.dialogue_renderer
    assert isinstance(dialogue, ElevenLabsDialogueRenderer)
    assert (dialogue.host_voice_id, dialogue.cohost_voice_id) == (
        DEFAULT_VOICE_ID,
        DEFAULT_COHOST_VOICE_ID,
    )


def test_agent_voice_plan_uses_the_chosen_voices(chorus_home: Path) -> None:
    from chorus import host_mode
    from chorus.onboarding import save_config

    save_config(_eleven(voice=Voice.host_plugin, host_voice_id=ARIA.voice_id))
    assert host_mode.principal_voices()["host"] == ARIA.voice_id

