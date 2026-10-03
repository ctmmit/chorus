"""Build pipeline `Deps` from onboarding choices instead of from whichever
keys happen to be in the environment.

`chorus.pipeline.default_deps` picks real or mock providers by key presence,
which is right for the hosted API and tests. A local install has an explicit
answer to "who thinks" and "who voices", so a missing key for a chosen
provider is an error here, never a silent fall back to the mock. State lives
under `~/.chorus/` (`chorus.paths`).
"""
from __future__ import annotations

import os

from chorus import paths
from chorus.artifacts import LocalArtifactStore
from chorus.audio import (
    AudioRenderer,
    ElevenLabsDialogueRenderer,
    ElevenLabsRenderer,
    MockAudioRenderer,
    ProfileAwareRenderer,
)
from chorus.llm import AnthropicLLMClient, LLMClient, MockLLMClient
from chorus.onboarding import (
    ANTHROPIC_KEY,
    ELEVENLABS_KEY,
    TRANSCRIPT_KEYS,
    Brain,
    OnboardingConfig,
    Voice,
)
from chorus.pipeline import Deps
from chorus.script import AnthropicScriptComposer, MockScriptComposer, ScriptComposer
from chorus.transcript_cache import SqliteTranscriptCache


class BrainConfigError(RuntimeError):
    """The configured brain or voice cannot be built; the message says why."""


def _key(env: str) -> str:
    value = os.environ.get(env, "").strip()
    if not value:
        raise BrainConfigError(f"{env} is not set; run `chorus onboard --reset keys`")
    return value


def build_thinking(config: OnboardingConfig) -> tuple[LLMClient, ScriptComposer]:
    if config.brain is Brain.anthropic:
        key = _key(ANTHROPIC_KEY.env)
        return AnthropicLLMClient(key), AnthropicScriptComposer(key)
    if config.brain is Brain.mock:
        return MockLLMClient(), MockScriptComposer()
    if config.brain is Brain.host:
        raise BrainConfigError(
            "the 'host' brain runs inside your coding agent over MCP, not from `chorus run`"
        )
    raise BrainConfigError("no brain chosen; run `chorus onboard`")


def build_voice(config: OnboardingConfig) -> AudioRenderer:
    if config.voice is Voice.elevenlabs_key:
        key = _key(ELEVENLABS_KEY.env)
        return ProfileAwareRenderer(
            ElevenLabsRenderer(key, out_dir=paths.artifacts_dir()),
            ElevenLabsDialogueRenderer(key),
        )
    if config.voice is Voice.text_only:
        mock = MockAudioRenderer(out_dir=paths.artifacts_dir())
        return ProfileAwareRenderer(mock, mock)
    if config.voice is Voice.host_plugin:
        raise BrainConfigError(
            "the 'host-plugin' voice runs through your coding agent's ElevenLabs MCP"
        )
    raise BrainConfigError("no voice chosen; run `chorus onboard`")


def restrict_transcript_env(config: OnboardingConfig) -> list[str]:
    """Drop, for this process only, every paid transcript key the principal did
    not choose. `build_transcript_chain` enables a rung whenever its key is in
    the environment, and a legacy `.env.local` copied into `~/.chorus/.env`
    may still carry keys for providers the principal declined. Returns the
    variables removed."""
    chosen = TRANSCRIPT_KEYS.get(config.transcripts) if config.transcripts else None
    removed = []
    for spec in TRANSCRIPT_KEYS.values():
        if spec is not chosen and os.environ.pop(spec.env, None) is not None:
            removed.append(spec.env)
    return removed


def build_local_deps(config: OnboardingConfig) -> Deps:
    # Imported here: build_transcript_chain pulls in the whole provider stack.
    from chorus.config_env import build_transcript_chain

    restrict_transcript_env(config)
    llm, composer = build_thinking(config)
    renderer = build_voice(config)
    return Deps(
        provider=build_transcript_chain(SqliteTranscriptCache(paths.db_path())),
        llm=llm,
        composer=composer,
        renderer=renderer,
        artifacts=LocalArtifactStore(paths.artifacts_dir()),
    )
