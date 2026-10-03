"""The voices on a principal's ElevenLabs account, for the onboarding voices step.

`GET /v2/voices` lists every voice the key can use: the premade library plus
the account's own clones and designs. Listing needs the key's voices-read
permission, so a restricted key fails here; onboarding then falls back to a
pasted voice id rather than giving up.
"""
from __future__ import annotations

import re
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, Field

VOICES_URL = "https://api.elevenlabs.io/v2/voices"
PAGE_SIZE = 100  # the endpoint's maximum
MAX_PAGES = 5  # 500 voices is more than any account shows a person in a list
LIST_TIMEOUT_SECONDS = 15.0
# ElevenLabs voice ids are opaque alphanumeric strings (20 characters today).
VOICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{15,40}$")
_SUMMARY_LABELS = ("accent", "gender", "age", "use_case", "descriptive")


class VoiceInfo(BaseModel):
    voice_id: str
    name: str
    category: str | None = Field(default=None, description="premade, cloned, generated, ...")
    description: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    preview_url: str | None = None

    def summary(self) -> str:
        """`Aria (american, female, narration; premade)`."""
        traits = [self.labels[k].replace("_", " ") for k in _SUMMARY_LABELS if self.labels.get(k)]
        detail = ", ".join(traits)
        if self.category:
            detail = f"{detail}; {self.category}" if detail else self.category
        return f"{self.name} ({detail})" if detail else self.name


class VoiceListError(RuntimeError):
    """The account's voices could not be listed; the message says why."""


class InvalidVoiceId(ValueError):
    """Not something ElevenLabs would accept as a voice id."""


class VoiceCatalog(Protocol):
    def list_voices(self) -> list[VoiceInfo]: ...


def check_voice_id(voice_id: str) -> str:
    cleaned = voice_id.strip()
    if not VOICE_ID_PATTERN.fullmatch(cleaned):
        raise InvalidVoiceId(
            f"{voice_id!r} is not an ElevenLabs voice id (letters and digits, like "
            "21m00Tcm4TlvDq8ikWAM); copy it from the voice's page in ElevenLabs"
        )
    return cleaned


class ElevenLabsVoiceCatalog:
    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        self.api_key = api_key
        self._client = client

    def list_voices(self) -> list[VoiceInfo]:
        if self._client is not None:
            return self._list(self._client)
        with httpx.Client(timeout=LIST_TIMEOUT_SECONDS) as client:
            return self._list(client)

    def _list(self, client: httpx.Client) -> list[VoiceInfo]:
        voices: list[VoiceInfo] = []
        token: str | None = None
        for _ in range(MAX_PAGES):
            params: dict[str, str | int] = {"page_size": PAGE_SIZE, "include_total_count": "false"}
            if token:
                params["next_page_token"] = token
            page = self._page(client, params)
            voices += [VoiceInfo.model_validate(v) for v in page.get("voices", [])]
            token = page.get("next_page_token")
            if not page.get("has_more") or not token:
                break
        return sorted(voices, key=lambda v: v.name.casefold())

    def _page(self, client: httpx.Client, params: dict[str, str | int]) -> dict[str, Any]:
        try:
            resp = client.get(VOICES_URL, params=params, headers={"xi-api-key": self.api_key})
        except httpx.HTTPError as err:
            raise VoiceListError(f"could not reach ElevenLabs to list voices: {err}") from err
        if resp.status_code in (401, 403):
            raise VoiceListError(
                f"the ElevenLabs key cannot list voices (HTTP {resp.status_code}); it may lack "
                "the voices-read permission. Paste a voice id from ElevenLabs instead."
            )
        if resp.status_code != 200:
            raise VoiceListError(f"ElevenLabs returned HTTP {resp.status_code} listing voices")
        body = resp.json()
        if not isinstance(body, dict):
            raise VoiceListError("ElevenLabs returned an unexpected voice list")
        return body
