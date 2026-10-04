"""`chorus`: the local-agent command line.

    chorus onboard [--reset STEP ...]   guided setup; resumes where it stopped
    chorus status                       what is configured and what blocks a run
    chorus run [--episode ID ...]       digest this week's episodes (or the given ones)
    chorus update [--check] [--yes]     check for and apply a new Chorus release
    chorus register [--host H] [--yes]  connect Chorus to Claude Code, Claude Desktop, Codex, Grok
    chorus schedule on|off|status       run the digest weekly with the OS scheduler
    chorus setup <action> ...           the same onboarding as JSON, for an agent driving
                                        Chorus from a shell (see `chorus setup --help`)

Environment and state are loaded before anything else is imported, so every
provider sees the keys saved in `~/.chorus/.env`.
"""
from __future__ import annotations

import argparse
import sys

from chorus import paths
from chorus.config import load_env

EXIT_OK = 0
EXIT_NOT_READY = 2
EXIT_FAILED = 1


def _parser() -> argparse.ArgumentParser:
    from chorus.onboarding import Step

    parser = argparse.ArgumentParser(prog="chorus", description="Chorus local agent")
    commands = parser.add_subparsers(dest="command", required=True)
    onboard = commands.add_parser("onboard", help="guided setup (resumes where it stopped)")
    onboard.add_argument(
        "--reset",
        action="append",
        default=[],
        choices=[s.value for s in Step],
        metavar="STEP",
        help="redo a step: " + ", ".join(s.value for s in Step),
    )
    commands.add_parser("status", help="show configuration and readiness")
    register = commands.add_parser("register", help="connect Chorus to your agents over MCP")
    register.add_argument(
        "--host",
        action="append",
        default=[],
        choices=["claude-code", "claude-desktop", "codex", "grok"],
        help="only these hosts (default: every one found)",
    )
    register.add_argument("--list", action="store_true", help="show the changes; apply none")
    register.add_argument("--yes", action="store_true", help="apply without asking")
    schedule = commands.add_parser("schedule", help="weekly digest via the OS scheduler")
    schedule.add_argument("action", choices=["on", "off", "status"])
    schedule.add_argument("--day", default="mon", help="mon..sun (default mon)")
    schedule.add_argument("--time", default="08:00", help="24-hour HH:MM (default 08:00)")
    update = commands.add_parser("update", help="check for and apply a new Chorus release")
    update.add_argument("--check", action="store_true", help="only report; change nothing")
    update.add_argument("--yes", action="store_true", help="apply a breaking update unprompted")
    commands.add_parser("feed", help="write a podcast feed of your finished digests")
    run = commands.add_parser("run", help="digest this week's episodes")
    run.add_argument(
        "--episode",
        action="append",
        default=[],
        metavar="ID",
        help="digest these episode ids instead of this week's shows and feeds",
    )
    _add_setup(commands)
    return parser


def _add_setup(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    setup = commands.add_parser(
        "setup",
        help="JSON onboarding for agents without MCP",
        description="Every action prints one JSON object. Errors print {\"error\": ...} and "
        "exit 2. Start with `chorus setup status` and follow `next`.",
    )
    actions = setup.add_subparsers(dest="action", required=True)
    actions.add_parser("status", help="readiness and the next step to walk through")
    options = actions.add_parser("options", help="the prompt and options for one step")
    options.add_argument("step")
    choose = actions.add_parser("set", help="record a choice-step answer")
    choose.add_argument("step")
    choose.add_argument("value")
    key = actions.add_parser("key", help="store an API key read from stdin (keeps it off argv)")
    key.add_argument("env")
    actions.add_parser("voices", help="the ElevenLabs voices to pick from")
    voice = actions.add_parser("voice", help="record the chosen voices (no ids: the defaults)")
    voice.add_argument("--host", help="host voice id")
    voice.add_argument("--host-name")
    voice.add_argument("--cohost", help="second voice id, for two-host episodes")
    voice.add_argument("--cohost-name")
    draft = actions.add_parser("soul-draft", help="draft and validate a soul without saving")
    draft.add_argument(
        "--source", required=True, choices=["interview", "write", "corpus", "preset", "file"]
    )
    draft.add_argument("--answers", help="interview answers as a JSON file ('-' for stdin)")
    draft.add_argument("--preset")
    draft.add_argument("--file", help="soul markdown to validate ('-' for stdin)")
    draft.add_argument("--corpus", help="file or folder of .md/.txt notes")
    save = actions.add_parser("soul-save", help="save the approved soul")
    save.add_argument("name")
    save.add_argument("--file", required=True, help="soul markdown ('-' for stdin)")
    actions.add_parser("soul-show", help="print the current soul")
    shows = actions.add_parser("shows", help="set followed shows and feeds")
    shows.add_argument("--show", action="append", default=[])
    shows.add_argument("--feed", action="append", default=[])
    shows.add_argument("--weekly", action="store_true")
    smoke = actions.add_parser("smoke", help="run (or --skip) the test digest")
    smoke.add_argument("--skip", action="store_true")
    smoke.add_argument("--agent-model", help="host brain: the model you are")
    run = actions.add_parser("run", help="digest this week's episodes; waits for the result")
    run.add_argument("--episode", action="append", default=[])
    # Host brain: the calling agent scores and scripts; Chorus validates.
    host_start = actions.add_parser(
        "host-start", help="host brain: fetch transcripts and open a run you drive"
    )
    host_start.add_argument("--episode", action="append", default=[])
    host_start.add_argument("--format", default="monologue", choices=["monologue", "dialogue"])
    host_start.add_argument("--agent-model", help="the model you are, recorded with the run")
    host_next = actions.add_parser("host-next", help="host brain: what to do now for a run")
    host_next.add_argument("job_id")
    host_episode = actions.add_parser("host-episode", help="host brain: one episode's windows")
    host_episode.add_argument("job_id")
    host_episode.add_argument("episode_id")
    host_scores = actions.add_parser("host-scores", help="host brain: submit an episode's scores")
    host_scores.add_argument("job_id")
    host_scores.add_argument("episode_id")
    host_scores.add_argument("--file", required=True, help='{"scores": [...]} JSON (- for stdin)')
    host_script = actions.add_parser("host-script", help="host brain: submit the script")
    host_script.add_argument("job_id")
    host_script.add_argument(
        "--file", required=True, help='{"takes": [...], "turns": [...]} JSON (- for stdin)'
    )
    host_audio = actions.add_parser("host-audio", help="agent voice: submit the voiced chunks")
    host_audio.add_argument("job_id")
    audio_input = host_audio.add_mutually_exclusive_group(required=True)
    audio_input.add_argument(
        "--file", help='{"chunks": [{"index": 0, "path": "..."}]} JSON (- for stdin)'
    )
    audio_input.add_argument("--skip", metavar="REASON", help="could not voice it; deliver text")


def _read_arg(value: str) -> str:
    from pathlib import Path

    if value == "-":
        return sys.stdin.read()
    return Path(value).expanduser().read_text(encoding="utf-8")


def cmd_setup(args: argparse.Namespace) -> int:
    import json
    from pathlib import Path

    from pydantic import BaseModel

    from chorus import agent_setup
    from chorus.brains import BrainConfigError
    from chorus.wizard import _read_corpus

    try:
        action = args.action
        result: object
        if action == "status":
            result = agent_setup.agent_status()
        elif action == "options":
            result = agent_setup.step_options(args.step)
        elif action == "set":
            result = agent_setup.set_choice(args.step, args.value)
        elif action == "key":
            result = agent_setup.set_key(args.env, sys.stdin.readline())
        elif action == "voices":
            result = agent_setup.list_voices()
        elif action == "voice":
            result = agent_setup.set_voice(args.host, args.cohost, args.host_name, args.cohost_name)
        elif action == "soul-draft":
            answers = json.loads(_read_arg(args.answers)) if args.answers else None
            texts = list(_read_corpus(Path(args.corpus).expanduser())) if args.corpus else None
            markdown = _read_arg(args.file) if args.file else None
            result = agent_setup.soul_draft(args.source, answers, texts, args.preset, markdown)
        elif action == "soul-save":
            result = agent_setup.soul_save(args.name, _read_arg(args.file))
        elif action == "soul-show":
            result = agent_setup.soul_show()
        elif action == "shows":
            result = agent_setup.set_shows(args.show, args.feed, args.weekly)
        elif action == "smoke":
            result = agent_setup.smoke_test(
                run=not args.skip, background=False, agent_model=args.agent_model
            )
        elif action == "run":
            result = agent_setup.run_digest(args.episode or None)
        else:
            result = _host_action(args)
    except (ValueError, KeyError, BrainConfigError, OSError) as err:
        print(json.dumps({"error": str(err)}))
        return EXIT_NOT_READY
    payload = result.model_dump(mode="json") if isinstance(result, BaseModel) else result
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return EXIT_OK


def _host_action(args: argparse.Namespace) -> object:
    import json

    from chorus import host_mode, paths
    from chorus.jobs import SqliteJobStore
    from chorus.local_run import recent_episodes
    from chorus.models import EpisodeInput
    from chorus.onboarding import load_config

    store = SqliteJobStore(paths.db_path())
    try:
        if args.action == "host-start":
            config = load_config()
            episodes = (
                [EpisodeInput(video_id=i) for i in args.episode]
                if args.episode
                else recent_episodes(config)
            )
            job_id = host_mode.start(
                store,
                config,
                episodes,
                episode_format=args.format,
                brain_model=args.agent_model,
                background=False,
            )
            return host_mode.next_task(store, job_id)
        if args.action == "host-next":
            return host_mode.next_task(store, args.job_id)
        if args.action == "host-episode":
            return host_mode.episode_windows(args.job_id, args.episode_id)
        if args.action == "host-audio":
            if args.skip is not None:
                return host_mode.submit_audio(store, args.job_id, skip_reason=args.skip)
            chunks = json.loads(_read_arg(args.file))["chunks"]
            return host_mode.submit_audio(store, args.job_id, chunks)
        body = json.loads(_read_arg(args.file))
        if args.action == "host-scores":
            return host_mode.submit_scores(store, args.job_id, args.episode_id, body["scores"])
        return host_mode.submit_script(
            store, load_config(), args.job_id, body.get("takes", []), body.get("turns")
        )
    finally:
        store.close()


def cmd_update(check_only: bool, assume_yes: bool) -> int:
    from chorus.updater import apply_update
    from chorus.version import check

    info = check(force=True)
    if info.error:
        print(info.error)
        return EXIT_FAILED
    if not info.update_available:
        latest = f" (latest release: {info.latest})" if info.latest else ""
        print(f"Chorus {info.installed} is up to date{latest}.")
        return EXIT_OK
    kind = "a breaking update" if info.breaking else "an update"
    print(f"Chorus {info.latest} is {kind} (installed: {info.installed}). What changed:")
    for release in info.changes:
        print(f"\n## {release.version}\n{release.notes or '(no notes)'}")
    if check_only:
        return EXIT_OK
    if info.breaking and not assume_yes:
        if not sys.stdin.isatty():
            print("\nThis is a breaking update. Rerun with --yes to apply it.")
            return EXIT_NOT_READY
        if input("\nApply this breaking update? (y/N): ").strip().lower() not in {"y", "yes"}:
            print("Not applied.")
            return EXIT_OK
    outcome = apply_update(info.install_kind)
    for step in outcome.steps:
        print(f"$ {step}")
    print(outcome.message)
    return EXIT_OK if outcome.applied or not outcome.steps else EXIT_FAILED


def cmd_onboard(resets: list[str]) -> int:
    from chorus.onboarding import Step, load_config, status
    from chorus.wizard import Wizard

    try:
        config = Wizard().run(load_config(), [Step(r) for r in resets])
    except (KeyboardInterrupt, EOFError):
        print("\nStopped. Progress is saved; rerun `chorus onboard` to continue.")
        return EXIT_NOT_READY
    return EXIT_OK if status(config).ready else EXIT_NOT_READY


def cmd_status() -> int:
    from chorus.onboarding import load_config, status, voices_apply
    from chorus.version import status_check

    config = load_config()
    current = status(config)
    print(f"Config: {current.config_path}")
    print(
        f"Mode: {config.mode or '-'}   Brain: {config.brain or '-'}   "
        f"Voice: {config.voice or '-'}   Transcripts: {config.transcripts or '-'}"
    )
    print(f"Soul: {config.soul or '-'}   Shows: {len(config.shows)}   Feeds: {len(config.feeds)}")
    if voices_apply(config):
        host = config.host_voice_name or config.host_voice_id or "default"
        cohost = config.cohost_voice_name or config.cohost_voice_id or "default"
        print(f"Voices: host {host}   co-host {cohost}")
    info = status_check(config.updates.value if config.updates else None)
    if info is not None and info.update_available:
        kind = "breaking update" if info.breaking else "update"
        print(f"Chorus {info.latest} is available ({kind}); run `{info.update_command}`.")
    for row in current.steps:
        mark = "x" if row.done else " "
        note = f"  ({row.blocker})" if row.blocker else ""
        print(f"  [{mark}] {row.step.value}{note}")
    if current.ready:
        print("Ready. `chorus run` digests this week's episodes.")
        return EXIT_OK
    print(f"Not ready. Next: `chorus onboard` ({current.next_step}).")
    return EXIT_NOT_READY


def cmd_run(episode_ids: list[str]) -> int:
    from chorus.brains import BrainConfigError
    from chorus.local_run import recent_episodes, run_digest, write_digest_markdown
    from chorus.models import EpisodeInput, JobStatus
    from chorus.onboarding import OnboardingError, load_config
    from chorus.wizard import summarize

    config = load_config()
    episodes = (
        [EpisodeInput(video_id=i) for i in episode_ids] if episode_ids else recent_episodes(config)
    )
    if not episodes:
        print("No new episodes in the past week from your shows and feeds.")
        return EXIT_OK
    print(f"Digesting {len(episodes)} episode(s)...")
    try:
        job = run_digest(config, episodes)
    except (OnboardingError, BrainConfigError) as err:
        print(err)
        return EXIT_NOT_READY
    print(summarize(job))
    print(f"Digest saved to {write_digest_markdown(job)}")
    return EXIT_OK if job.status is JobStatus.done else EXIT_FAILED


def cmd_register(hosts: list[str], list_only: bool, assume_yes: bool) -> int:
    from chorus.registration import Host, detect, register

    wanted = {Host(h) for h in hosts}
    failures = 0
    for target in detect():
        if wanted and target.host not in wanted:
            continue
        if not target.found:
            print(f"{target.label}: not found")
            continue
        if target.registered:
            print(f"{target.label}: already registered")
            continue
        where = f" in {target.location}" if target.location else ""
        print(f"{target.label}: {target.change}{where}")
        if list_only:
            continue
        if not assume_yes:
            if not sys.stdin.isatty():
                print("  Skipped (not a terminal). Rerun with --yes to apply.")
                continue
            if input("  Apply? (Y/n): ").strip().lower() not in {"", "y", "yes"}:
                continue
        result = register(target.host)
        backup = f" (backup: {result.backup})" if result.backup else ""
        print(f"  {result.message}{backup}")
        failures += 0 if result.ok else 1
    return EXIT_FAILED if failures else EXIT_OK


def cmd_schedule(action: str, day: str, time: str) -> int:
    from chorus import os_schedule
    from chorus.onboarding import Brain, Voice, load_config

    if action == "status":
        on = os_schedule.is_scheduled()
        print("Weekly digest is scheduled." if on else "Weekly digest is not scheduled.")
        return EXIT_OK
    if action == "off":
        print(os_schedule.remove())
        return EXIT_OK
    config = load_config()
    if config.brain is Brain.host or config.voice is Voice.host_plugin:
        print(os_schedule.AGENT_SCHEDULE_NOTE)
        return EXIT_NOT_READY
    try:
        plan = os_schedule.plan(day, time)
        print(os_schedule.apply(plan))
    except os_schedule.ScheduleError as err:
        print(f"Could not schedule: {err}")
        return EXIT_FAILED
    print(f"It runs: {' '.join(plan.run_command)}")
    return EXIT_OK


def cmd_feed() -> int:
    from chorus.jobs import SqliteJobStore
    from chorus.podcast_feed import LOCAL_FEED_NAME, write_local_feed

    store = SqliteJobStore(paths.db_path())
    try:
        out, count = write_local_feed(store, paths.artifacts_dir(), paths.home() / LOCAL_FEED_NAME)
    finally:
        store.close()
    print(f"Wrote {count} episode(s) to {out}")
    print("Open it in a desktop podcast player. On a phone, use the hosted service's feed")
    print("URL instead (GET /feed or the get_podcast_feed tool); a local file is not reachable.")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    from chorus.migrations import migrate_config
    from chorus.onboarding import OnboardingError

    paths.ensure_home()
    load_env()
    try:
        migration = migrate_config()
    except OnboardingError as err:
        print(err)
        return EXIT_NOT_READY
    if migration is not None:
        print(
            f"Upgraded your settings from v{migration.from_version} to v{migration.to_version} "
            f"(backup: {migration.backup})."
        )
    args = _parser().parse_args(argv)
    if args.command == "update":
        return cmd_update(args.check, args.yes)
    if args.command == "register":
        return cmd_register(args.host, args.list, args.yes)
    if args.command == "schedule":
        return cmd_schedule(args.action, args.day, args.time)
    if args.command == "onboard":
        return cmd_onboard(args.reset)
    if args.command == "status":
        return cmd_status()
    if args.command == "setup":
        return cmd_setup(args)
    if args.command == "feed":
        return cmd_feed()
    return cmd_run(args.episode)


if __name__ == "__main__":
    sys.exit(main())
