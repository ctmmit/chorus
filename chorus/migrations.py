"""Upgrade a principal's saved state when a new Chorus changes its shape.

`~/.chorus/config.toml` carries `schema_version`. When an update bumps
`chorus.onboarding.CONFIG_SCHEMA_VERSION`, it adds a step to
`CONFIG_MIGRATIONS` (from-version -> function returning the next version's
dict). `migrate_config` applies the steps in order, after copying the old
file to `~/.chorus/backups/`, and never edits a file it cannot fully migrate.

Souls are not rewritten here: they are the principal's prose. If an update
adds a required soul section, `chorus.onboarding.status` reports the soul
step as blocked with what is missing, and onboarding reopens at that step.

Runs at the start of every local entrypoint (`chorus`, `chorus-mcp`), so an
update takes effect the next time Chorus starts.
"""
from __future__ import annotations

import shutil
import tomllib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from chorus import paths
from chorus.onboarding import (
    CONFIG_SCHEMA_VERSION,
    OnboardingConfig,
    OnboardingError,
    save_config,
)

ConfigMigration = Callable[[dict[str, Any]], dict[str, Any]]

# from_version -> step to from_version + 1. Empty while the schema is v1.
CONFIG_MIGRATIONS: dict[int, ConfigMigration] = {}


class MigrationResult(BaseModel):
    from_version: int
    to_version: int
    backup: str | None = None


def migrate_config(
    path: Path | None = None,
    migrations: Mapping[int, ConfigMigration] | None = None,
    target: int = CONFIG_SCHEMA_VERSION,
) -> MigrationResult | None:
    """Migrate config.toml in place. None when there is no file or it is
    already current."""
    steps = CONFIG_MIGRATIONS if migrations is None else migrations
    file = path or paths.config_path()
    if not file.is_file():
        return None
    raw: dict[str, Any] = tomllib.loads(file.read_text(encoding="utf-8"))
    version = int(raw.get("schema_version", 1))
    if version == target:
        return None
    if version > target:
        raise OnboardingError(
            f"{file} is schema v{version}, newer than this Chorus (v{target}); update Chorus"
        )
    missing = [v for v in range(version, target) if v not in steps]
    if missing:
        raise OnboardingError(f"no migration from config schema v{missing[0]}; reinstall Chorus")
    for step_from in range(version, target):
        raw = steps[step_from](dict(raw))
        raw["schema_version"] = step_from + 1
    migrated = OnboardingConfig.model_validate(raw)  # validate before touching the file
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = paths.backups_dir() / f"config-v{version}-{stamp}.toml"
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(file, backup)
    save_config(migrated, file)
    return MigrationResult(from_version=version, to_version=target, backup=str(backup))
