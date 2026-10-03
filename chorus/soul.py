"""Soul validation, presets, and the local soul library.

Onboarding refuses to finish, and the local runner refuses to curate, until
the principal has a soul that passes `validate_soul`. Validation is about
structure, not taste: the lens must say who the principal is, what makes a
segment worth surfacing, what to skip, and how high the bar sits. Those four
sections are what curation and the refusal path actually lean on.

Section names are matched loosely because souls arrive from several sources
(interview, corpus, a pasted file, the bundled presets) and the presets
already use shorter headings ("Identity", "Ignore", "Communication style").
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

from chorus import paths
from chorus.curation import soul_version
from chorus.models import MAX_SOUL_CHARS

SOUL_SCHEMA_VERSION = 1
SOUL_FILE_SUFFIX = ".md"
_SOUL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


class SoulSection(StrEnum):
    identity = "Identity & Role"
    core_interests = "Core Interests"
    attention_triggers = "Attention Triggers"
    anti_interests = "Anti-interests"
    taste = "Taste & Sensibility"
    guidance = "Curation Guidance"


REQUIRED_SECTIONS = (
    SoulSection.identity,
    SoulSection.attention_triggers,
    SoulSection.anti_interests,
    SoulSection.guidance,
)
RECOMMENDED_SECTIONS = (SoulSection.core_interests, SoulSection.taste)

# Lower-cased heading prefixes accepted for each section, checked in order.
_ALIASES: dict[SoulSection, tuple[str, ...]] = {
    SoulSection.identity: ("identity",),
    SoulSection.core_interests: ("core interests", "interests"),
    SoulSection.attention_triggers: ("attention trigger", "triggers"),
    SoulSection.anti_interests: ("anti-interest", "anti interest", "ignore", "skip"),
    SoulSection.taste: ("taste", "communication style", "style", "sensibility"),
    SoulSection.guidance: ("curation guidance", "guidance"),
}

# The six interview questions, keyed exactly as `build_from_interview` and the
# chorus-soul-bootstrap skill expect.
INTERVIEW_QUESTIONS: tuple[tuple[str, str], ...] = (
    ("identity", "What is your role, and what decisions are you responsible for?"),
    ("interests", "Which topics or questions are you actively pursuing? (comma-separated)"),
    (
        "triggers",
        "What would make you stop and save a podcast segment? Specific ideas, claims, "
        "people, or kinds of evidence. (comma-separated)",
    ),
    ("ignore", "What subjects, tropes, or levels of discussion should Chorus skip? (comma-separated)"),
    (
        "style",
        "What intellectual style do you value: empirical, contrarian, technical, practical, "
        "narrative, or something else?",
    ),
    (
        "guidance",
        "Calibrate the bar. When should Chorus surface a segment, and when should it "
        "refuse rather than pad the digest?",
    ),
)

PRESETS: dict[str, str] = {
    "investor": "soul_investor.md",
    "popculture": "soul_popculture.md",
}


class SoulCheck(BaseModel):
    valid: bool
    version: str
    missing: list[str]
    empty: list[str]
    warnings: list[str]


class SoulError(ValueError):
    """A soul that cannot be saved or used; the message says what to fix."""


def _classify(heading: str) -> SoulSection | None:
    lowered = heading.lower()
    for section, prefixes in _ALIASES.items():
        if any(lowered.startswith(prefix) for prefix in prefixes):
            return section
    return None


def parse_sections(markdown: str) -> dict[SoulSection, str]:
    """Map each recognised `## ` heading to its body text. A later duplicate
    heading appends to the earlier one rather than replacing it."""
    found: dict[SoulSection, str] = {}
    matches = list(_HEADING_RE.finditer(markdown))
    for index, match in enumerate(matches):
        section = _classify(match.group(1))
        if section is None:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        body = markdown[match.end() : end].strip()
        found[section] = f"{found[section]}\n{body}".strip() if section in found else body
    return found


def _is_empty(body: str) -> bool:
    meaningful = [
        line
        for line in body.splitlines()
        if line.strip() and line.strip().lstrip("-* ").lower() not in {"(none inferred)", ""}
    ]
    return not meaningful


def validate_soul(markdown: str) -> SoulCheck:
    sections = parse_sections(markdown)
    missing = [s.value for s in REQUIRED_SECTIONS if s not in sections]
    # Anti-interests may legitimately be "(none inferred)"; every other
    # required section must say something.
    empty = [
        s.value
        for s in REQUIRED_SECTIONS
        if s in sections and s is not SoulSection.anti_interests and _is_empty(sections[s])
    ]
    warnings = [f"no '{s.value}' section" for s in RECOMMENDED_SECTIONS if s not in sections]
    if len(markdown) > MAX_SOUL_CHARS:
        empty.append(f"soul is {len(markdown)} chars; the limit is {MAX_SOUL_CHARS}")
    return SoulCheck(
        valid=not missing and not empty,
        version=soul_version(markdown),
        missing=missing,
        empty=empty,
        warnings=warnings,
    )


def describe_problems(check: SoulCheck) -> str:
    parts = []
    if check.missing:
        parts.append("missing sections: " + ", ".join(check.missing))
    if check.empty:
        parts.append("empty: " + ", ".join(check.empty))
    return "; ".join(parts) or "ok"


def load_preset(name: str) -> str:
    filename = PRESETS.get(name)
    if filename is None:
        raise SoulError(f"unknown preset {name!r}; choose one of {', '.join(PRESETS)}")
    return (paths.fixtures_dir() / "souls" / filename).read_text(encoding="utf-8")


def check_name(name: str) -> str:
    if not _SOUL_NAME_RE.fullmatch(name):
        raise SoulError(
            f"soul name {name!r} must be lowercase letters, digits, '-' or '_' (max 64 chars)"
        )
    return name


def soul_path(name: str) -> Path:
    return paths.souls_dir() / f"{check_name(name)}{SOUL_FILE_SUFFIX}"


def save_soul(name: str, markdown: str) -> SoulCheck:
    """Validate and write `~/.chorus/souls/<name>.md`. An existing soul with
    different content is backed up first; souls are never silently lost."""
    check = validate_soul(markdown)
    if not check.valid:
        raise SoulError(f"soul is not usable yet ({describe_problems(check)})")
    target = soul_path(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.read_text(encoding="utf-8") != markdown:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = paths.backups_dir() / f"soul-{name}-{stamp}{SOUL_FILE_SUFFIX}"
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    target.write_text(markdown, encoding="utf-8")
    return check


def load_soul(name: str) -> str:
    target = soul_path(name)
    if not target.is_file():
        raise SoulError(f"no soul named {name!r} at {target}")
    return target.read_text(encoding="utf-8")
