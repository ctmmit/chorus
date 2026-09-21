from __future__ import annotations

import httpx
import pytest

from chorus.email import RESEND_API_URL, ResendEmailSender


def test_resend_sender_posts_documented_request_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_post(
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, str | list[str]],
        timeout: float,
    ) -> httpx.Response:
        captured.update(url=url, headers=headers, json=json, timeout=timeout)
        return httpx.Response(
            200,
            json={"id": "email-id"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    sender = ResendEmailSender("re_test", "Chorus <hello@example.com>")

    sender.send("principal@example.com", "Your key", "plain text", "<p>HTML</p>")

    assert captured["url"] == RESEND_API_URL
    assert captured["headers"] == {"Authorization": "Bearer re_test"}
    assert captured["json"] == {
        "from": "Chorus <hello@example.com>",
        "to": ["principal@example.com"],
        "subject": "Your key",
        "text": "plain text",
        "html": "<p>HTML</p>",
    }
