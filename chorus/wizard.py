"""`chorus onboard`: the terminal front end over `chorus.onboarding`.

All I/O goes through `WizardIO`, so tests drive the wizard with scripted
answers and no terminal. Re-running resumes: finished steps are skipped
unless reset with `chorus onboard --reset <step>`.
"""
from __future__ import annotations

import getpass
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from chorus import paths
from chorus.bootstrap import AnthropicSoulBuilder, MockSoulBuilder, SoulBuilder
from chorus.models import Job, JobStatus
from chorus.onboarding import (
    ANTHROPIC_KEY,
    STEP_ORDER,
    STEP_TITLES,
    Brain,
    Mode,
    OnboardingConfig,
    OnboardingError,
    Step,
    Voice,
    apply,
    mark_done,
    options,
    required_keys,
    reset,
    save_config,
    set_env_value,
    status,
)
from chorus.soul import (
    INTERVIEW_QUESTIONS,
    PRESETS,
    SoulError,
    check_name,
    describe_problems,
    load_preset,
    save_soul,
    soul_path,
    validate_soul,
)

DEFAULT_SOUL_NAME = "me"
CORPUS_SUFFIXES = (".md", ".txt")
MAX_CORPUS_FILES = 200


@dataclass
class WizardIO:
    ask: Callable[[str], str] = input
    secret: Callable[[str], str] = getpass.getpass
    say: Callable[[str], None] = print


@dataclass
class Wizard:
    io: WizardIO = field(default_factory=WizardIO)
    smoke: Callable[[OnboardingConfig], Job] | None = None
    soul_builder: Callable[[OnboardingConfig], SoulBuilder] | None = None

    # --- prompts -----------------------------------------------------------

    def _ask(self, prompt: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        answer = self.io.ask(f"{prompt}{suffix}: ").strip()
        return answer or default

    def _confirm(self, prompt: str, default: bool) -> bool:
        hint = "Y/n" if default else "y/N"
        while True:
            answer = self.io.ask(f"{prompt} ({hint}): ").strip().lower()
            if not answer:
                return default
            if answer in {"y", "yes"}:
                return True
            if answer in {"n", "no"}:
                return False
            self.io.say("  Please answer y or n.")

    def _choose(self, step: Step, config: OnboardingConfig) -> str:
        offered = options(step, config)
        self.io.say("")
        for number, option in enumerate(offered, start=1):
            mark = "" if option.available else f"  (unavailable: {option.reason})"
            self.io.say(f"  {number}. {option.label}{mark}")
            self.io.say(f"     {option.detail}")
        first = next(i for i, o in enumerate(offered, start=1) if o.available)
        while True:
            raw = self._ask("Choose", str(first))
            if raw.isdigit() and 1 <= int(raw) <= len(offered):
                picked = offered[int(raw) - 1]
                if picked.available:
                    return picked.value
                self.io.say(f"  {picked.label} is unavailable: {picked.reason}")
                continue
            self.io.say(f"  Enter a number from 1 to {len(offered)}.")

    # --- steps -------------------------------------------------------------

    def run(self, config: OnboardingConfig, resets: list[Step] | None = None) -> OnboardingConfig:
        paths.ensure_home()
        for step in resets or []:
            config = reset(config, step)
        self.io.say("Chorus onboarding. Answers are saved as you go; rerun to resume.")
        self.io.say(f"State lives in {paths.home()}")
        for step in STEP_ORDER:
            if step in config.completed:
                continue
            self.io.say("")
            self.io.say(f"== {STEP_TITLES[step]}")
            config = self._run_step(step, config)
            save_config(config)
        self._finish(config)
        return config

    def _run_step(self, step: Step, config: OnboardingConfig) -> OnboardingConfig:
        if step is Step.keys:
            return self._keys(config)
        if step is Step.soul:
            return self._soul(config)
        if step is Step.shows:
            return self._shows(config)
        if step is Step.smoke_test:
            return self._smoke(config)
        if step is Step.brain and config.mode is Mode.host_agent:
            self.io.say(
                "  Letting your coding agent do the thinking is coming in a later release. "
                "For now choose a key or the demo; you can switch later."
            )
        while True:
            try:
                return apply(step, self._choose(step, config), config)
            except OnboardingError as err:
                self.io.say(f"  {err}")

    def _keys(self, config: OnboardingConfig) -> OnboardingConfig:
        needed = required_keys(config)
        if not needed:
            self.io.say("  Your choices need no API keys.")
            return mark_done(config, Step.keys)
        self.io.say(f"  Keys are stored in {paths.env_path()}, never in the repo.")
        for spec in needed:
            if os.environ.get(spec.env, "").strip():
                self.io.say(f"  {spec.label}: found {spec.env}.")
                continue
            self.io.say(f"  {spec.label}: get a key at {spec.url}")
            value = self.io.secret(f"  Paste {spec.env} (input hidden, Enter to skip): ").strip()
            if not value:
                self.io.say(f"  Skipped. Chorus cannot run until {spec.env} is set.")
                continue
            set_env_value(paths.env_path(), spec.env, value)
            os.environ[spec.env] = value
            self.io.say(f"  Saved {spec.env}.")
        if any(not os.environ.get(s.env, "").strip() for s in needed):
            return config
        return mark_done(config, Step.keys)

    def _builder(self, config: OnboardingConfig) -> SoulBuilder:
        if self.soul_builder is not None:
            return self.soul_builder(config)
        key = os.environ.get(ANTHROPIC_KEY.env, "").strip()
        if config.brain is Brain.anthropic and key:
            return AnthropicSoulBuilder(key)
        return MockSoulBuilder()

    def _soul(self, config: OnboardingConfig) -> OnboardingConfig:
        self.io.say(
            "  The soul is a short markdown lens: who you are, what makes a segment worth "
            "your time, what to skip, and how high the bar sits. Chorus will not run without one."
        )
        while True:
            try:
                default_name = config.soul or DEFAULT_SOUL_NAME
                name = check_name(self._ask("  Name for this soul", default_name))
                break
            except SoulError as err:
                self.io.say(f"  {err}")
        sources = [
            ("interview", "Answer six questions"),
            ("file", "Use an existing soul.md file"),
            ("corpus", "Derive it from your notes or reading (a file or folder of .md/.txt)"),
            ("preset", "Start from a preset (" + ", ".join(PRESETS) + ")"),
        ]
        for number, (_, label) in enumerate(sources, start=1):
            self.io.say(f"  {number}. {label}")
        while True:
            raw = self._ask("  Choose", "1")
            if raw.isdigit() and 1 <= int(raw) <= len(sources):
                break
        source = sources[int(raw) - 1][0]
        draft = self._draft(source, config)
        markdown = self._review(name, draft)
        check = save_soul(name, markdown)
        self.io.say(f"  Saved {soul_path(name)} (version {check.version}).")
        for warning in check.warnings:
            self.io.say(f"  Note: {warning}. Curation works without it, but it sharpens the lens.")
        return mark_done(config.model_copy(update={"soul": name}), Step.soul)

    def _draft(self, source: str, config: OnboardingConfig) -> str:
        if source == "interview":
            answers = {key: self._ask(f"  {question}") for key, question in INTERVIEW_QUESTIONS}
            return self._builder(config).build_from_interview(answers)
        if source == "file":
            while True:
                path = Path(self._ask("  Path to soul.md")).expanduser()
                if path.is_file():
                    return path.read_text(encoding="utf-8")
                self.io.say(f"  No file at {path}.")
        if source == "corpus":
            while True:
                texts = list(_read_corpus(Path(self._ask("  File or folder")).expanduser()))
                if texts:
                    self.io.say(f"  Read {len(texts)} file(s).")
                    return self._builder(config).derive_from_corpus(texts)
                self.io.say("  Found no .md or .txt files there.")
        while True:
            preset = self._ask("  Preset", next(iter(PRESETS)))
            try:
                return load_preset(preset)
            except SoulError as err:
                self.io.say(f"  {err}")

    def _review(self, name: str, draft: str) -> str:
        """Show the draft and loop until it validates and the principal
        approves it. Editing happens in a draft file beside the soul."""
        draft_file = paths.souls_dir() / f"{name}.draft.md"
        markdown = draft
        while True:
            check = validate_soul(markdown)
            self.io.say("")
            self.io.say(markdown.rstrip())
            self.io.say("")
            if check.valid and self._confirm("  Use this soul?", default=True):
                draft_file.unlink(missing_ok=True)
                return markdown
            if not check.valid:
                self.io.say(f"  Not usable yet: {describe_problems(check)}.")
            draft_file.write_text(markdown, encoding="utf-8")
            self.io.ask(f"  Edit {draft_file}, save it, then press Enter: ")
            markdown = draft_file.read_text(encoding="utf-8")

    def _shows(self, config: OnboardingConfig) -> OnboardingConfig:
        from chorus.catalog import list_shows

        names = [str(s["show"]) for s in list_shows()]
        if names:
            self.io.say("  Bundled catalog shows:")
            for number, show in enumerate(names, start=1):
                self.io.say(f"  {number}. {show}")
            picked = self._ask("  Numbers to follow, comma-separated (Enter for none)")
            chosen = [
                names[int(p) - 1]
                for p in (x.strip() for x in picked.split(","))
                if p.isdigit() and 1 <= int(p) <= len(names)
            ]
        else:
            chosen = []
        feeds_raw = self._ask("  Podcast RSS feed URLs, comma-separated (Enter for none)")
        feeds = [f.strip() for f in feeds_raw.split(",") if f.strip()]
        bad = [f for f in feeds if not f.startswith(("http://", "https://"))]
        if bad:
            self.io.say(f"  Ignoring entries that are not http(s) URLs: {', '.join(bad)}")
            feeds = [f for f in feeds if f not in bad]
        weekly = self._confirm("  Run a digest every week?", default=False)
        if weekly:
            self.io.say(
                "  Noted. Automatic weekly scheduling arrives in a later release; until then "
                "run `chorus run` (or schedule it with Task Scheduler or cron)."
            )
        updated = config.model_copy(update={"shows": chosen, "feeds": feeds, "weekly": weekly})
        return mark_done(updated, Step.shows)

    def _smoke(self, config: OnboardingConfig) -> OnboardingConfig:
        # The sample transcript is a bundled fixture, so transcript keys are
        # never touched; only the brain and the voice can spend money here.
        billed = config.brain is Brain.anthropic or config.voice is Voice.elevenlabs_key
        prompt = "  Run one test digest on a bundled sample transcript?"
        if billed:
            prompt += " It makes real, billed API calls (a few cents)."
        if not self._confirm(prompt, default=not billed):
            self.io.say("  Skipped. Run `chorus onboard --reset smoke_test` to try later.")
            return mark_done(config, Step.smoke_test)
        runner = self.smoke or _default_smoke
        try:
            job = runner(config)
        except Exception as err:  # noqa: BLE001 - report any provider failure to the principal
            self.io.say(f"  Smoke test could not run: {type(err).__name__}: {err}")
            return config
        self.io.say(summarize(job))
        if job.status is not JobStatus.done:
            return config
        return mark_done(config, Step.smoke_test)

    def _finish(self, config: OnboardingConfig) -> None:
        current = status(config)
        self.io.say("")
        if not current.ready:
            pending = [f"{r.step} ({r.blocker or 'not done'})" for r in current.steps if not r.done]
            self.io.say("Onboarding is not finished: " + "; ".join(pending))
            self.io.say("Rerun `chorus onboard` to continue.")
            return
        self.io.say("Chorus is set up.")
        if config.mode is Mode.host_agent:
            self.io.say(_agent_registration_hint())
        else:
            self.io.say("Run `chorus run` for this week's digest; `chorus status` shows the setup.")


def _default_smoke(config: OnboardingConfig) -> Job:
    from chorus.local_run import smoke_test

    return smoke_test(config)


def _read_corpus(path: Path) -> Iterator[str]:
    if path.is_file():
        yield path.read_text(encoding="utf-8", errors="replace")
        return
    if not path.is_dir():
        return
    files = sorted(p for p in path.rglob("*") if p.suffix.lower() in CORPUS_SUFFIXES)
    for file in files[:MAX_CORPUS_FILES]:
        yield file.read_text(encoding="utf-8", errors="replace")


def summarize(job: Job) -> str:
    from chorus.local_run import artifact_file

    lines = [f"  Status: {job.status.value}"]
    if job.error:
        lines.append(f"  Error: {job.error}")
    for episode in job.digest.episodes if job.digest else []:
        if episode.refused:
            lines.append(f"  {episode.episode_id}: refused ({episode.refusal_reason})")
            continue
        for h in episode.highlights:
            lines.append(f"  {episode.episode_id} @ {h.segment_timestamp:.0f}s: \"{h.quote}\"")
    audio = artifact_file(job)
    if audio is not None:
        lines.append(f"  Episode file: {audio}")
    lines += [f"  Warning: {w}" for w in job.warnings]
    return "\n".join(lines)


def _agent_registration_hint() -> str:
    return (
        "Register Chorus with your coding agent, then ask it to run your digest:\n"
        "  Claude Code:  claude mcp add --scope user chorus -- chorus-mcp\n"
        "  Codex:        add to ~/.codex/config.toml\n"
        "                [mcp_servers.chorus]\n"
        '                command = "chorus-mcp"\n'
        "Use the absolute path to chorus-mcp if it is not on your PATH."
    )
