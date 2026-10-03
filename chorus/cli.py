"""`chorus`: the local-agent command line.

    chorus onboard [--reset STEP ...]   guided setup; resumes where it stopped
    chorus status                       what is configured and what blocks a run
    chorus run [--episode ID ...]       digest this week's episodes (or the given ones)

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
    return parser


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
    return cmd_run(args.episode)


if __name__ == "__main__":
    sys.exit(main())
