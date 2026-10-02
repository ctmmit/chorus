from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_root_and_installable_chorus_skill_are_byte_identical() -> None:
    assert (ROOT / "SKILL.md").read_bytes() == (ROOT / "skills/chorus/SKILL.md").read_bytes()
