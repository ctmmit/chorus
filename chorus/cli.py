"""`chorus`: the local-agent command line.

    chorus onboard [--reset STEP ...]   guided setup; resumes where it stopped
    chorus status                       what is configured and what blocks a run
    chorus run [--episode ID ...]       digest this week's episodes (or the given ones)
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
    run = actions.add_parser("run", help="digest this week's episodes; waits for the result")
    run.add_argument("--episode", action="append", default=[])


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
            result = agent_setup.smoke_test(run=not args.skip)
        else:
            result = agent_setup.run_digest(args.episode or None)
    except (ValueError, BrainConfigError, OSError) as err:
        print(json.dumps({"error": str(err)}))
        return EXIT_NOT_READY
    payload = result.model_dump(mode="json") if isinstance(result, BaseModel) else result
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return EXIT_OK


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
    from chorus.onboarding import load_config, status

    config = load_config()
    current = status(config)
    print(f"Config: {current.config_path}")
    print(
        f"Mode: {config.mode or '-'}   Brain: {config.brain or '-'}   "
        f"Voice: {config.voice or '-'}   Transcripts: {config.transcripts or '-'}"
    )
    print(f"Soul: {config.soul or '-'}   Shows: {len(config.shows)}   Feeds: {len(config.feeds)}")
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
    from chorus.local_run import recent_episodes, run_digest
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
    return EXIT_OK if job.status is JobStatus.done else EXIT_FAILED


def main(argv: list[str] | None = None) -> int:
    paths.ensure_home()
    load_env()
    args = _parser().parse_args(argv)
    if args.command == "onboard":
        return cmd_onboard(args.reset)
    if args.command == "status":
        return cmd_status()
    if args.command == "setup":
        return cmd_setup(args)
    return cmd_run(args.episode)


if __name__ == "__main__":
    sys.exit(main())
