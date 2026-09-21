"""Transactional-email seam for self-serve Chorus API keys."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import httpx

RESEND_API_URL = "https://api.resend.com/emails"
RESEND_API_KEY_ENV = "RESEND_API_KEY"
CHORUS_EMAIL_FROM_ENV = "CHORUS_EMAIL_FROM"
EMAIL_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class SentEmail:
    to: str
    subject: str
    text: str
    html: str | None = None
    headers: dict[str, str] | None = None


@runtime_checkable
class EmailSender(Protocol):
    def send(
        self,
        to: str,
        subject: str,
        text: str,
        html: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        """`headers` (Phase F) carries custom message headers such as
        `List-Unsubscribe` / `List-Unsubscribe-Post` for bulk-sender
        compliance (Gmail/Yahoo)."""
        ...


class MockEmailSender:
    def __init__(self) -> None:
        self.sent: list[SentEmail] = []

    def send(
        self,
        to: str,
        subject: str,
        text: str,
        html: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.sent.append(SentEmail(to=to, subject=subject, text=text, html=html, headers=headers))


class ResendEmailSender:
    def __init__(self, api_key: str, from_address: str) -> None:
        self._api_key = api_key
        self._from_address = from_address

    def send(
        self,
        to: str,
        subject: str,
        text: str,
        html: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        # Resend's `headers` field (verified 21 Sep 2026 against
        # resend.com/docs/api-reference/emails/send-email): a flat object of
        # header name -> value, e.g. {"List-Unsubscribe": "<https://...>"}.
        payload: dict[str, str | list[str] | dict[str, str]] = {
            "from": self._from_address,
            "to": [to],
            "subject": subject,
            "text": text,
        }
        if html is not None:
            payload["html"] = html
        if headers:
            payload["headers"] = headers
        response = httpx.post(
            RESEND_API_URL,
            headers={"Authorization": f"Bearer {self._api_key}"},
            json=payload,
            timeout=EMAIL_TIMEOUT_SECONDS,
        )
        response.raise_for_status()


def get_email_sender() -> EmailSender:
    api_key = os.environ.get(RESEND_API_KEY_ENV)
    from_address = os.environ.get(CHORUS_EMAIL_FROM_ENV)
    if api_key and from_address:
        return ResendEmailSender(api_key, from_address)
    return MockEmailSender()
