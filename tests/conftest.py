"""Shared test configuration.

The real show transcripts (fixtures/transcripts/<video_id>.json) are NOT in the
public repo — they are captions of commercial podcasts and live in the private
fixtures repo (see fixtures/README.md). Tests that depend on them are skipped
when the files are absent; the synthetic `sample_public` transcript keeps the
whole spine covered in every environment.

Detection is per test, not per module: a test is skipped when its source
mentions a private episode id, or a module-level name whose value is (or
contains) one — so a mostly-mocked module like test_transcripts keeps running
and only its fixture-backed cases skip.
"""
from __future__ import annotations

import inspect
from collections.abc import Iterator
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
TRANSCRIPTS = FIXTURES / "transcripts"

# The private show transcripts, plus the deliberately caption-less episode that
# tests reference alongside them (see fixtures/episodes.json).
PRIVATE_IDS = frozenset(
    {
        "2Ryr95iiYNk",
        "c4tvVKDhpiY",
        "gs39QFYIbBY",
        "IAgmW_gTxls",
        "wAnDWfEIwoE",
        "xKZ_8ULR91Y",
        "KhZfxZ-C-2g",
    }
)
PRIVATE_SENTINEL = TRANSCRIPTS / "c4tvVKDhpiY.json"
HAVE_PRIVATE_FIXTURES = PRIVATE_SENTINEL.exists()

requires_private_fixtures = pytest.mark.skipif(
    not HAVE_PRIVATE_FIXTURES,
    reason="private show transcripts not present (run scripts/sync_private_fixtures)",
)


def _mentions_private_id(value: object) -> bool:
    if isinstance(value, str):
        return value in PRIVATE_IDS
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_mentions_private_id(v) for v in value)
    return False


def _private_names(module: object) -> set[str]:
    """Module-level constants bound to a private id (e.g. ANDREESSEN, CLEAN)."""
    return {
        name
        for name, value in vars(module).items()
        if not name.startswith("__") and _mentions_private_id(value)
    }


def _uses_private_fixtures(item: pytest.Item) -> bool:
    function = getattr(item, "function", None)
    module = getattr(item, "module", None)
    if function is None or module is None:
        return False
    try:
        source = inspect.getsource(function)
    except (OSError, TypeError):
        return True  # cannot inspect: be conservative, skip without fixtures
    if any(pid in source for pid in PRIVATE_IDS):
        return True
    names = _private_names(module)
    # Helpers like `_resolve(ANDREESSEN)` or `_payload(*CLEAN)` reference the
    # constant by name; a bare substring check on the source is enough here.
    return any(name in source for name in names)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if HAVE_PRIVATE_FIXTURES:
        return
    for item in items:
        if _uses_private_fixtures(item):
            item.add_marker(requires_private_fixtures)


@pytest.fixture
def chorus_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """An isolated ~/.chorus with no provider keys. The checkout's real
    .env.local and chorus.db are never read or copied."""
    from chorus import paths

    home = tmp_path / "chorus-home"
    monkeypatch.setenv(paths.CHORUS_HOME_ENV, str(home))
    for env in (
        "ANTHROPIC_API_KEY",
        "ELEVENLABS_API_KEY",
        "ASSEMBLYAI_API_KEY",
        "DEEPGRAM_API_KEY",
        "TRANSCRIPT_API_KEY",
    ):
        monkeypatch.delenv(env, raising=False)
    # Never read or copy the checkout's real .env.local / chorus.db in tests.
    monkeypatch.setattr(paths, "_copy_legacy", lambda source, target: None)
    monkeypatch.setattr("chorus.cli.load_env", lambda: None)
    home.mkdir()
    for sub in ("souls", "artifacts", "backups", "extensions"):
        (home / sub).mkdir()
    yield home
