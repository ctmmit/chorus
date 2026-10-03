"""Onboarding for a principal's own agent, whatever that agent is.

A new user hands the GitHub link to their primary agent: Claude Code, Claude
Cowork, Codex, Grok Build, OpenClaw, Hermes, Muse, or anything else that can
run a command or speak MCP. Those hosts share no config format and some have
no MCP at all, so this module is transport-neutral. Every function takes and
returns plain JSON-able data, and two thin transports expose it:

- MCP tools on the local stdio server (`chorus.mcp_server`, `onboarding_*`)
- the JSON CLI (`chorus setup ...`, `chorus.cli`) for shell-only hosts

The wording an agent should use with its principal comes back in each
response (`StepPrompt.ask`, `.agent_notes`). Instructions live in the
installed package, not in a skill file copied into the host, so they improve
when Chorus updates and every host gets the same ones.

The soul step stays mandatory: `status().ready` is false until a validated
soul is saved, and `run` refuses until then.
"""
from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel

from chorus import paths
from chorus.bootstrap import AnthropicSoulBuilder, MockSoulBuilder, SoulBuilder
from chorus.models import EpisodeInput, Job
from chorus.onboarding import (
    ANTHROPIC_KEY,
    STEP_TITLES,
    TRANSCRIPT_KEYS,
    Brain,
    Mode,
    OnboardingConfig,
    OnboardingError,
    Option,
    Status,
    Step,
    Voice,
    apply,
    load_config,
    mark_done,
    missing_keys,
    options,
    required_keys,
    save_config,
    set_env_value,
    status,
)
from chorus.soul import (
    INTERVIEW_QUESTIONS,
    PRESETS,
    SoulCheck,
    check_name,
    load_preset,
    save_soul,
    soul_path,
    validate_soul,
)

SETUP_PROTOCOL = 1
SETTABLE_KEYS = frozenset(
    [ANTHROPIC_KEY.env, "ELEVENLABS_API_KEY", *(k.env for k in TRANSCRIPT_KEYS.values())]
)
SOUL_SOURCES = ("interview", "write", "corpus", "preset", "file")


class StepPrompt(BaseModel):
    step: Step
    title: str
    kind: str  # choice | keys | soul | shows | confirm
    ask: str
    options: list[Option] = []
    agent_notes: list[str] = []
    data: dict[str, Any] = {}


class AgentStatus(BaseModel):
    protocol: int = SETUP_PROTOCOL
    ready: bool
    status: Status
    next: StepPrompt | None
    state_dir: str


class SoulDraft(BaseModel):
    markdown: str
    check: SoulCheck
    agent_notes: list[str]


# --- prompts ---------------------------------------------------------------

_COMMON_NOTES = [
    "Ask the principal; never choose on their behalf. Offer the options in plain words.",
    "After each answer call the matching set tool, then onboarding_status again for the next step.",
]

_ASK: dict[Step, str] = {
    Step.mode: "Should Chorus run on its own on this machine, or through me as your agent?",
    Step.brain: "Who should do the thinking: picking the segments worth your time and writing "
    "the episode script?",
    Step.voice: "Who should voice the episode?",
    Step.transcripts: "Where should podcast transcripts come from? Paid speech-to-text covers "
    "nearly every show; free sources cover only a few.",
    Step.updates: "When a new version of Chorus comes out, what should happen?",
}


def step_prompt(step: Step, config: OnboardingConfig) -> StepPrompt:
    title = STEP_TITLES[step]
    if step in _ASK:
        notes = list(_COMMON_NOTES)
        if step is Step.mode:
            notes.append(
                "You are an agent driving setup, so 'host-agent' usually fits; it still has to "
                "be the principal's call."
            )
        if step is Step.brain and config.mode is Mode.host_agent:
            notes.append(
                "Options marked unavailable are on the roadmap. Mention them, but only offer "
                "the available ones."
            )
        return StepPrompt(
            step=step,
            title=title,
            kind="choice",
            ask=_ASK[step],
            options=options(step, config),
            agent_notes=notes,
        )
    if step is Step.keys:
        missing = set(missing_keys(config))
        keys = [
            {**k.model_dump(), "present": k.env not in missing} for k in required_keys(config)
        ]
        return StepPrompt(
            step=step,
            title=title,
            kind="keys",
            ask="Your choices need these API keys. Each one is stored only on this machine.",
            agent_notes=[
                "Key values pasted into this conversation stay in its transcript. Say so, and "
                f"offer the alternative: the principal adds lines like ENV=value to "
                f"{paths.env_path()} themselves, then you call onboarding_status again.",
                "If they paste a key, pass it to onboarding_set_key immediately and never repeat "
                "it back.",
                "If a key is skipped, Chorus cannot run; offer to change the brain or voice "
                "choice to one that needs no key instead.",
            ],
            data={"keys": keys, "env_file": str(paths.env_path())},
        )
    if step is Step.soul:
        return StepPrompt(
            step=step,
            title=title,
            kind="soul",
            ask="Chorus curates through your 'soul': a short description of who you are, what "
            "makes a podcast segment worth your time, what to skip, and how high the bar sits. "
            "How would you like to build it?",
            agent_notes=[
                "Sources: 'interview' (ask the six questions one at a time and keep the "
                "principal's own words), 'write' (you draft it from what you already know about "
                "them, in the template below), 'corpus' (read their notes or saved articles and "
                "pass the texts), 'preset' (start from a bundled lens), 'file' (an existing "
                "soul.md).",
                "Get a draft from onboarding_soul_draft, or write the markdown yourself.",
                "Show the principal the whole draft. Revise until they approve it, then call "
                "onboarding_soul_save. Never save a soul they have not seen.",
                "Do not invent preferences. Ask one focused follow-up when answers conflict.",
            ],
            data={
                "questions": [{"key": k, "question": q} for k, q in INTERVIEW_QUESTIONS],
                "presets": list(PRESETS),
                "template_sections": [
                    "Identity & Role",
                    "Core Interests",
                    "Attention Triggers",
                    "Anti-interests",
                    "Taste & Sensibility",
                    "Curation Guidance",
                ],
                "current": config.soul,
            },
        )
    if step is Step.shows:
        from chorus.catalog import list_shows

        return StepPrompt(
            step=step,
            title=title,
            kind="shows",
            ask="Which podcasts should Chorus follow? Name shows from the catalog, give RSS feed "
            "URLs, or both. And should it digest them every week?",
            agent_notes=[
                "Use search_podcasts / resolve_podcast to turn a show name into its RSS feed URL "
                "when it is not in the catalog.",
                "Then call onboarding_set_shows.",
            ],
            data={"catalog_shows": [s["show"] for s in list_shows()]},
        )
    billed = config.brain is Brain.anthropic or config.voice is Voice.elevenlabs_key
    return StepPrompt(
        step=step,
        title=title,
        kind="confirm",
        ask="Run one test digest on a bundled sample transcript to check everything works?"
        + (" It makes real, billed API calls (a few cents)." if billed else ""),
        agent_notes=[
            "Call onboarding_smoke_test(run=true) on yes, or run=false to skip.",
            "Share the highlights it returns, and where the episode file was saved.",
        ],
        data={"billed": billed},
    )


# --- status and choices ----------------------------------------------------


def _refresh(config: OnboardingConfig) -> OnboardingConfig:
    """The keys step completes itself once nothing required is missing."""
    chosen = config.brain and config.voice and config.transcripts
    if chosen and Step.keys not in config.completed and not missing_keys(config):
        return mark_done(config, Step.keys)
    return config


def _load() -> OnboardingConfig:
    paths.ensure_home()
    config = load_config()
    refreshed = _refresh(config)
    if refreshed != config:
        save_config(refreshed)
    return refreshed


def agent_status() -> AgentStatus:
    config = _load()
    current = status(config)
    next_prompt = step_prompt(current.next_step, config) if current.next_step else None
    return AgentStatus(
        ready=current.ready, status=current, next=next_prompt, state_dir=str(paths.home())
    )


def step_options(step: str) -> StepPrompt:
    return step_prompt(Step(step), _load())


def set_choice(step: str, value: str) -> AgentStatus:
    save_config(_refresh(apply(Step(step), value, _load())))
    return agent_status()


def set_key(env: str, value: str) -> AgentStatus:
    if env not in SETTABLE_KEYS:
        expected = ", ".join(sorted(SETTABLE_KEYS))
        raise OnboardingError(f"{env} is not a key Chorus stores; expected one of {expected}")
    set_env_value(paths.env_path(), env, value.strip())
    os.environ[env] = value.strip()
    save_config(_refresh(_load()))
    return agent_status()


def set_shows(shows: list[str], feeds: list[str], weekly: bool) -> AgentStatus:
    from chorus.catalog import list_shows

    known = {s["show"] for s in list_shows()}
    unknown = [s for s in shows if s not in known]
    if unknown:
        raise OnboardingError(
            f"not in the catalog: {', '.join(unknown)}. Pass them as RSS feed URLs instead "
            "(resolve_podcast finds a show's feed)."
        )
    bad = [f for f in feeds if not f.startswith(("http://", "https://"))]
    if bad:
        raise OnboardingError(f"feed URLs must be http(s): {', '.join(bad)}")
    config = _load().model_copy(update={"shows": shows, "feeds": feeds, "weekly": weekly})
    save_config(mark_done(config, Step.shows))
    return agent_status()


# --- soul ------------------------------------------------------------------


def _builder(config: OnboardingConfig) -> SoulBuilder:
    key = os.environ.get(ANTHROPIC_KEY.env, "").strip()
    if config.brain is Brain.anthropic and key:
        return AnthropicSoulBuilder(key)
    return MockSoulBuilder()


def soul_draft(
    source: str,
    answers: dict[str, str] | None = None,
    texts: list[str] | None = None,
    preset: str | None = None,
    markdown: str | None = None,
) -> SoulDraft:
    """Build a draft soul without saving it. 'write' and 'file' just validate
    markdown the agent supplies (its own draft, or a file it read)."""
    config = _load()
    if source == "interview":
        draft = _builder(config).build_from_interview(answers or {})
    elif source == "corpus":
        if not texts:
            raise OnboardingError("source 'corpus' needs texts")
        draft = _builder(config).derive_from_corpus(texts)
    elif source == "preset":
        draft = load_preset(preset or next(iter(PRESETS)))
    elif source in {"write", "file"}:
        if not markdown:
            raise OnboardingError(f"source {source!r} needs markdown")
        draft = markdown
    else:
        raise OnboardingError(f"unknown soul source {source!r}; use one of {SOUL_SOURCES}")
    check = validate_soul(draft)
    notes = ["Show this draft to the principal in full and ask for changes or approval."]
    if not check.valid:
        notes.append(
            "Not usable yet: fill the missing or empty sections with the principal, then "
            "validate again via onboarding_soul_draft(source='write', markdown=...)."
        )
    if config.brain is not Brain.anthropic and source in {"interview", "corpus"}:
        notes.append(
            "This draft came from a plain template. You can write a richer one yourself from "
            "the same material (source='write'); keep the six section headings."
        )
    return SoulDraft(markdown=draft, check=check, agent_notes=notes)


def soul_save(name: str, markdown: str) -> AgentStatus:
    check_name(name)
    save_soul(name, markdown)
    config = _load().model_copy(update={"soul": name})
    save_config(mark_done(config, Step.soul))
    return agent_status()


def soul_show() -> dict[str, Any]:
    config = _load()
    if not config.soul:
        return {"name": None, "markdown": None}
    text = soul_path(config.soul).read_text(encoding="utf-8")
    return {"name": config.soul, "markdown": text, "check": validate_soul(text).model_dump()}


# --- smoke test and runs ---------------------------------------------------


def job_summary(job: Job) -> dict[str, Any]:
    from chorus.local_run import artifact_file

    episodes = []
    for episode in job.digest.episodes if job.digest else []:
        episodes.append(
            {
                "episode_id": episode.episode_id,
                "title": episode.episode_title,
                "refused": episode.refused,
                "refusal_reason": episode.refusal_reason,
                "highlights": [
                    {"at_seconds": h.segment_timestamp, "quote": h.quote, "why": h.why_surface}
                    for h in episode.highlights
                ],
            }
        )
    audio = artifact_file(job)
    return {
        "job_id": job.job_id,
        "status": job.status.value,
        "error": job.error,
        "episodes": episodes,
        "episode_file": str(audio) if audio else None,
        "warnings": job.warnings,
    }


def smoke_test(run: bool = True) -> dict[str, Any]:
    from chorus.local_run import smoke_test as run_smoke
    from chorus.models import JobStatus

    config = _load()
    if not run:
        save_config(mark_done(config, Step.smoke_test))
        return {"skipped": True, "status": agent_status().model_dump(mode="json")}
    unfinished = [
        r.step for r in status(config).steps if not r.done and r.step is not Step.smoke_test
    ]
    if unfinished:
        raise OnboardingError(f"finish these steps first: {', '.join(unfinished)}")
    job = run_smoke(config)
    if job.status is JobStatus.done:
        save_config(mark_done(config, Step.smoke_test))
    return {"result": job_summary(job), "status": agent_status().model_dump(mode="json")}


def run_digest(episode_ids: list[str] | None = None) -> dict[str, Any]:
    """Synchronous run over this week's configured shows and feeds, or the
    given episode ids (the CLI transport; MCP submits in the background)."""
    from chorus.local_run import recent_episodes
    from chorus.local_run import run_digest as run_local

    config = _load()
    episodes = (
        [EpisodeInput(video_id=i) for i in episode_ids] if episode_ids else recent_episodes(config)
    )
    if not episodes:
        return {"status": "nothing_new", "detail": "no new episodes in the past week"}
    return job_summary(run_local(config, episodes))
