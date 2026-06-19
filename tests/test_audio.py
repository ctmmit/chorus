"""Goal 3.2 — audio rendering (mock): produces a downloadable artifact."""
from __future__ import annotations

from pathlib import Path

from chorus.audio import MockAudioRenderer
from chorus.models import Script, Take


def _script() -> Script:
    takes = [Take(text="A take.", take_type="idea", episode_id="abc", segment_timestamp=42.0)]
    return Script(soul_version="deadbeef", takes=takes, monologue="A take.")


def test_mock_render_writes_downloadable_artifact(tmp_path: Path) -> None:
    path = MockAudioRenderer(out_dir=tmp_path).render(_script(), soul="x")
    assert path.exists()
    assert path.read_text(encoding="utf-8") == "A take."


def test_mock_render_names_by_soul_version(tmp_path: Path) -> None:
    path = MockAudioRenderer(out_dir=tmp_path).render(_script(), soul="x")
    assert "deadbeef" in path.name
