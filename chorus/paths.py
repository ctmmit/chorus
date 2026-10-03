"""The one place that knows where a local Chorus install keeps its state.

Rule (docs: onboarding plan, "Updates" Rule 1): everything personal lives
under `~/.chorus/` (or `$CHORUS_HOME`), never inside the code checkout or the
installed package. A `git pull` or a package upgrade then cannot conflict
with, or overwrite, a principal's soul, config, job history, or audio.

Only the local entrypoints use these locations (`chorus` CLI, `chorus-mcp`
stdio). The hosted API keeps selecting its stores from the environment
(`chorus.config_env`), and every library default (`chorus.jobs.DEFAULT_DB`,
`chorus.artifacts.ARTIFACT_DIR`) is unchanged, so tests stay hermetic.
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger("chorus.paths")

CHORUS_HOME_ENV = "CHORUS_HOME"
DEFAULT_HOME_DIRNAME = ".chorus"

CONFIG_FILENAME = "config.toml"
ENV_FILENAME = ".env"
DB_FILENAME = "chorus.db"
SOULS_DIRNAME = "souls"
ARTIFACTS_DIRNAME = "artifacts"
BACKUPS_DIRNAME = "backups"
EXTENSIONS_DIRNAME = "extensions"

# The checkout root, for a clone install. Used only to find legacy state the
# pre-onboarding layout wrote there, and the bundled fixtures.
REPO_ROOT = Path(__file__).resolve().parent.parent


def home() -> Path:
    override = os.environ.get(CHORUS_HOME_ENV)
    return Path(override).expanduser() if override else Path.home() / DEFAULT_HOME_DIRNAME


def config_path() -> Path:
    return home() / CONFIG_FILENAME


def env_path() -> Path:
    return home() / ENV_FILENAME


def db_path() -> Path:
    return home() / DB_FILENAME


def souls_dir() -> Path:
    return home() / SOULS_DIRNAME


def artifacts_dir() -> Path:
    return home() / ARTIFACTS_DIRNAME


def backups_dir() -> Path:
    return home() / BACKUPS_DIRNAME


def extensions_dir() -> Path:
    return home() / EXTENSIONS_DIRNAME


def fixtures_dir() -> Path:
    return REPO_ROOT / "fixtures"


def ensure_home() -> Path:
    """Create the state tree and copy (never move) legacy repo-root state the
    first time. Copying leaves a clone's working tree exactly as it was, so a
    contributor's existing dev database keeps working from the checkout too."""
    root = home()
    for directory in (root, souls_dir(), artifacts_dir(), backups_dir(), extensions_dir()):
        directory.mkdir(parents=True, exist_ok=True)
    _copy_legacy(REPO_ROOT / DB_FILENAME, db_path())
    _copy_legacy(REPO_ROOT / ".env.local", env_path())
    return root


def _copy_legacy(source: Path, target: Path) -> None:
    if target.exists() or not source.is_file():
        return
    shutil.copy2(source, target)
    log.info("paths: copied legacy %s to %s", source.name, target)
