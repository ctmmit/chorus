"""Onboarding state machine shared by every front end.

One set of steps, persisted in `~/.chorus/config.toml`, driven by either the
`chorus onboard` terminal wizard (`chorus.wizard`) or, later, by a coding
agent through MCP tools. Both front ends call the same pure functions here:
`options` says what a step offers (and why an option is unavailable),
`apply` records a choice, `status` reports what is done and what blocks a run.

The soul step is mandatory: `status().ready` is never true without a saved
soul that passes `chorus.soul.validate_soul`.
"""
from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from chorus import paths
from chorus.soul import SoulError, describe_problems, load_soul, validate_soul

CONFIG_SCHEMA_VERSION = 1
LATER_RELEASE = "arrives in a later Chorus release"


class Step(StrEnum):
    mode = "mode"
    brain = "brain"
    voice = "voice"
    transcripts = "transcripts"
    keys = "keys"
    soul = "soul"
    shows = "shows"
    updates = "updates"
    smoke_test = "smoke_test"


STEP_ORDER: tuple[Step, ...] = tuple(Step)

STEP_TITLES: dict[Step, str] = {
    Step.mode: "How will you run Chorus?",
    Step.brain: "Who does the thinking (scoring segments, writing the script)?",
    Step.voice: "Who voices the episode?",
    Step.transcripts: "Where do transcripts come from?",
    Step.keys: "API keys",
    Step.soul: "Your soul: the lens Chorus curates through (required)",
    Step.shows: "Shows and schedule",
    Step.updates: "Updates",
    Step.smoke_test: "Smoke test",
}


class Mode(StrEnum):
    local_agent = "local-agent"
    host_agent = "host-agent"


class Brain(StrEnum):
    host = "host"
    anthropic = "anthropic"
    mock = "mock"


class Voice(StrEnum):
    host_plugin = "host-plugin"
    elevenlabs_key = "elevenlabs-key"
    text_only = "text-only"


class Transcripts(StrEnum):
    free = "free"
    assemblyai = "assemblyai"
    deepgram = "deepgram"
    supadata = "supadata"


class UpdatePolicy(StrEnum):
    auto = "auto"
    notify = "notify"
    off = "off"


class OnboardingConfig(BaseModel):
    schema_version: int = CONFIG_SCHEMA_VERSION
    mode: Mode | None = None
    brain: Brain | None = None
    voice: Voice | None = None
    transcripts: Transcripts | None = None
    soul: str | None = Field(default=None, description="Name of the soul in ~/.chorus/souls/.")
    shows: list[str] = Field(default_factory=list, description="Catalog show names.")
    feeds: list[str] = Field(default_factory=list, description="Podcast RSS feed URLs.")
    weekly: bool = False
    updates: UpdatePolicy | None = None
    channel: str = "stable"
    completed: list[Step] = Field(default_factory=list)


class Option(BaseModel):
    value: str
    label: str
    detail: str
    available: bool = True
    reason: str | None = None


class KeySpec(BaseModel):
    env: str
    label: str
    url: str


class StepStatus(BaseModel):
    step: Step
    title: str
    done: bool
    blocker: str | None = None


class Status(BaseModel):
    ready: bool
    next_step: Step | None
    steps: list[StepStatus]
    missing_keys: list[str]
    config_path: str


class OnboardingError(ValueError):
    """A choice that cannot be applied; the message says why."""


ANTHROPIC_KEY = KeySpec(
    env="ANTHROPIC_API_KEY", label="Anthropic", url="https://console.anthropic.com/settings/keys"
)
ELEVENLABS_KEY = KeySpec(
    env="ELEVENLABS_API_KEY", label="ElevenLabs", url="https://elevenlabs.io/app/settings/api-keys"
)
TRANSCRIPT_KEYS: dict[Transcripts, KeySpec] = {
    Transcripts.assemblyai: KeySpec(
        env="ASSEMBLYAI_API_KEY", label="AssemblyAI", url="https://www.assemblyai.com/app/api-keys"
    ),
    Transcripts.deepgram: KeySpec(
        env="DEEPGRAM_API_KEY", label="Deepgram", url="https://console.deepgram.com/"
    ),
    Transcripts.supadata: KeySpec(
        env="TRANSCRIPT_API_KEY", label="Supadata", url="https://supadata.ai/"
    ),
}


# --- options ---------------------------------------------------------------


def options(step: Step, config: OnboardingConfig) -> list[Option]:
    if step is Step.mode:
        return [
            Option(
                value=Mode.local_agent,
                label="Local agent",
                detail="Chorus runs on this machine as your podcast agent (`chorus run`).",
            ),
            Option(
                value=Mode.host_agent,
                label="Inside a coding agent",
                detail="Codex, Claude Code or Grok Build drives Chorus over MCP.",
            ),
        ]
    if step is Step.brain:
        return [
            Option(
                value=Brain.host,
                label="The agent's own model",
                detail="Codex/Claude/Grok scores segments and writes the script. No Anthropic key.",
                available=False,
                reason=(
                    "needs mode 'host-agent'"
                    if config.mode is not Mode.host_agent
                    else LATER_RELEASE
                ),
            ),
            Option(
                value=Brain.anthropic,
                label="Anthropic API key",
                detail="Chorus calls Claude Haiku (scoring) and Sonnet (script, soul). Billed.",
            ),
            Option(
                value=Brain.mock,
                label="Demo (no key)",
                detail="Keyword scoring and a template script. Free; for trying the flow.",
            ),
        ]
    if step is Step.voice:
        return [
            Option(
                value=Voice.host_plugin,
                label="The agent's ElevenLabs plugin",
                detail="Chorus hands the agent a render plan; its ElevenLabs MCP voices it.",
                available=False,
                reason=(
                    "needs mode 'host-agent'"
                    if config.mode is not Mode.host_agent
                    else LATER_RELEASE
                ),
            ),
            Option(
                value=Voice.elevenlabs_key,
                label="ElevenLabs API key",
                detail="Chorus renders the mp3 itself, including native two-host dialogue. Billed.",
            ),
            Option(
                value=Voice.text_only,
                label="Text only",
                detail="No audio; the episode script is saved as text.",
            ),
        ]
    if step is Step.transcripts:
        return [
            Option(
                value=Transcripts.free,
                label="Free sources only",
                detail="Bundled fixtures plus feeds that publish their own transcript. "
                "Most shows will be skipped.",
            ),
            Option(
                value=Transcripts.assemblyai,
                label="AssemblyAI (recommended)",
                detail="Transcribes the feed's audio, with speaker labels. ~$0.23 per audio hour.",
            ),
            Option(
                value=Transcripts.deepgram,
                label="Deepgram",
                detail="Transcribes the feed's audio. Backup speech-to-text lineage.",
            ),
            Option(
                value=Transcripts.supadata,
                label="Supadata (YouTube captions)",
                detail="Cheapest, but no speaker labels and weaker terms footing. Last resort.",
            ),
        ]
    if step is Step.updates:
        return [
            Option(
                value=UpdatePolicy.notify,
                label="Tell me (recommended)",
                detail="Chorus reports new releases; you choose when to update.",
            ),
            Option(
                value=UpdatePolicy.auto,
                label="Automatic",
                detail="Take minor and patch releases silently; breaking ones always ask.",
            ),
            Option(value=UpdatePolicy.off, label="Off", detail="Never check for updates."),
        ]
    return []


_CHOICE_FIELDS: dict[Step, tuple[str, type[StrEnum]]] = {
    Step.mode: ("mode", Mode),
    Step.brain: ("brain", Brain),
    Step.voice: ("voice", Voice),
    Step.transcripts: ("transcripts", Transcripts),
    Step.updates: ("updates", UpdatePolicy),
}


def apply(step: Step, value: str, config: OnboardingConfig) -> OnboardingConfig:
    """Record a choice-step answer. Rejects values the step does not offer, or
    offers but marks unavailable."""
    if step not in _CHOICE_FIELDS:
        raise OnboardingError(f"step {step} is not a single-choice step")
    field, enum = _CHOICE_FIELDS[step]
    offered = {o.value: o for o in options(step, config)}
    option = offered.get(value)
    if option is None:
        raise OnboardingError(f"{value!r} is not an option for {step}; choose {', '.join(offered)}")
    if not option.available:
        raise OnboardingError(f"{option.label} is unavailable: {option.reason}")
    updated = config.model_copy(update={field: enum(value)})
    # Changing the mode can strand a brain/voice that needed the old mode.
    if step is Step.mode:
        updated = _drop_unavailable(updated)
    return mark_done(updated, step)


def _drop_unavailable(config: OnboardingConfig) -> OnboardingConfig:
    for step in (Step.brain, Step.voice):
        field, _ = _CHOICE_FIELDS[step]
        current = getattr(config, field)
        if current is None:
            continue
        available = {o.value for o in options(step, config) if o.available}
        if current not in available:
            config = reset(config, step)
    return config


def mark_done(config: OnboardingConfig, step: Step) -> OnboardingConfig:
    if step in config.completed:
        return config
    return config.model_copy(update={"completed": [*config.completed, step]})


def reset(config: OnboardingConfig, step: Step) -> OnboardingConfig:
    update: dict[str, object] = {"completed": [s for s in config.completed if s is not step]}
    if step in _CHOICE_FIELDS:
        update[_CHOICE_FIELDS[step][0]] = None
    elif step is Step.soul:
        update["soul"] = None
    return config.model_copy(update=update)


# --- keys ------------------------------------------------------------------


def required_keys(config: OnboardingConfig) -> list[KeySpec]:
    keys: list[KeySpec] = []
    if config.brain is Brain.anthropic:
        keys.append(ANTHROPIC_KEY)
    if config.voice is Voice.elevenlabs_key:
        keys.append(ELEVENLABS_KEY)
    if config.transcripts in TRANSCRIPT_KEYS:
        keys.append(TRANSCRIPT_KEYS[config.transcripts])
    return keys


def missing_keys(config: OnboardingConfig, environ: Mapping[str, str] | None = None) -> list[str]:
    env = os.environ if environ is None else environ
    return [k.env for k in required_keys(config) if not env.get(k.env, "").strip()]


def set_env_value(path: Path, key: str, value: str) -> None:
    """Insert or replace `key=value` in a dotenv file, leaving other lines
    untouched. The value is written unquoted; keys never contain spaces."""
    if not value or any(ch in value for ch in "\r\n"):
        raise OnboardingError(f"{key}: value is empty or spans lines")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out, replaced = [], False
    for line in lines:
        if line.split("=", 1)[0].strip() == key:
            if not replaced:
                out.append(f"{key}={value}")
                replaced = True
            continue
        out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    if os.name == "posix":
        path.chmod(0o600)


# --- status ----------------------------------------------------------------


def _soul_blocker(config: OnboardingConfig) -> str | None:
    if not config.soul:
        return "no soul yet"
    try:
        check = validate_soul(load_soul(config.soul))
    except SoulError as err:
        return str(err)
    return None if check.valid else describe_problems(check)


def status(config: OnboardingConfig, environ: Mapping[str, str] | None = None) -> Status:
    missing = missing_keys(config, environ)
    rows: list[StepStatus] = []
    for step in STEP_ORDER:
        blocker: str | None = None
        if step is Step.keys and missing:
            blocker = "missing " + ", ".join(missing)
        elif step is Step.soul:
            blocker = _soul_blocker(config)
        elif step in _CHOICE_FIELDS and getattr(config, _CHOICE_FIELDS[step][0]) is None:
            blocker = "not chosen"
        done = step in config.completed and blocker is None
        rows.append(StepStatus(step=step, title=STEP_TITLES[step], done=done, blocker=blocker))
    next_step = next((r.step for r in rows if not r.done), None)
    # The smoke test is a check, not a precondition: a run needs everything else.
    ready = all(r.done for r in rows if r.step is not Step.smoke_test)
    return Status(
        ready=ready,
        next_step=next_step,
        steps=rows,
        missing_keys=missing,
        config_path=str(paths.config_path()),
    )


# --- persistence -----------------------------------------------------------


def load_config(path: Path | None = None) -> OnboardingConfig:
    target = path or paths.config_path()
    if not target.exists():
        return OnboardingConfig()
    data = tomllib.loads(target.read_text(encoding="utf-8"))
    version = data.get("schema_version", CONFIG_SCHEMA_VERSION)
    if version > CONFIG_SCHEMA_VERSION:
        raise OnboardingError(
            f"{target} is schema v{version}, newer than this Chorus (v{CONFIG_SCHEMA_VERSION}); "
            "update Chorus"
        )
    return OnboardingConfig.model_validate(data)


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        # JSON string escapes are a subset of TOML basic-string escapes.
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(f"cannot write {type(value).__name__} to config.toml")


def dump_config(config: OnboardingConfig) -> str:
    data = config.model_dump(mode="json", exclude_none=True)
    lines = ["# Chorus local configuration. Edit with `chorus onboard`."]
    lines += [f"{key} = {_toml_value(value)}" for key, value in data.items()]
    return "\n".join(lines) + "\n"


def save_config(config: OnboardingConfig, path: Path | None = None) -> Path:
    target = path or paths.config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dump_config(config), encoding="utf-8")
    return target
