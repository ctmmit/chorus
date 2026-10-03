"""Environment loading for LIVE runs only.

Deliberately NOT imported anywhere in the package import path, so tests and
path_test stay hermetic (mocks unless a caller explicitly loads keys). Call
load_env() from a run entrypoint (serve / smoke / chorus CLI / chorus-mcp) to
activate real providers.

Two files are read, first match wins per variable (nothing overrides a value
already in the process environment): `~/.chorus/.env`, where `chorus onboard`
saves keys, then the checkout's `.env.local` for a contributor's clone.
"""
from __future__ import annotations

import os
from pathlib import Path

from chorus import paths

DEFAULT_ENV = paths.REPO_ROOT / ".env.local"


def _load_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        from dotenv import load_dotenv  # type: ignore[import-not-found]

        load_dotenv(path)
        return
    except ImportError:
        pass
    # Minimal fallback parser if python-dotenv isn't installed.
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_env(path: Path | None = None) -> None:
    """Load `path` alone when given (scripts that point at a specific file);
    otherwise the home env file, then the checkout's `.env.local`."""
    if path is not None:
        _load_file(path)
        return
    _load_file(paths.env_path())
    _load_file(DEFAULT_ENV)
