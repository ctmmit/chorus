"""Environment loading for LIVE runs only.

Deliberately NOT imported anywhere in the package import path, so tests and
path_test stay hermetic (mocks unless a caller explicitly loads keys). Call
load_env() from a run entrypoint (serve / smoke) to activate real providers.
"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ENV = Path(__file__).resolve().parent.parent / ".env.local"


def load_env(path: Path | None = None) -> None:
    path = path or DEFAULT_ENV
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
