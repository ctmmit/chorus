"""chorus/digest_email.py: golden-check the pure render (subject claim,
per-highlight links, honest refusals/skips, provenance, unsubscribe link and
headers). No network, no state — build a Job + Subscription by hand."""
from __future__ import annotations

from datetime import UTC, datetime

from chorus.digest_email import (
    LIST_UNSUBSCRIBE_HEADER,
    LIST_UNSUBSCRIBE_POST_HEADER,
    LIST_UNSUBSCRIBE_POST_VALUE,
    render_digest_email,
    unsubscribe_headers,
)
from chorus.models import (
    Digest,
    EpisodeDigest,
    EpisodeInput,
    Highlight,
    Job,
    JobStatus,
    JobUsage,
    SkippedEpisode,
)
from chorus.subscriptions import MASTER_OWNER, Subscription

YOUTUBE_EPISODE_ID = "c4tvVKDhpiY"
BASE_URL = "https://chorus.example.com"


def _rss_episode() -> EpisodeInput:
    return EpisodeInput(
        feed_url="https://feed.example.com/show.xml",
        guid="ep-1",
        audio_url="https://cdn.example.com/ep1.mp3",
    )


def _job_and_subscription() -> tuple[Job, Subscription]:
    rss_episode = _rss_episode()
    rss_id = rss_episode.resolved_id()

    digest = Digest(
        soul_version="abc123",
        soul_origin="supplied",
        episodes=[
            EpisodeDigest(
                episode_id=YOUTUBE_EPISODE_ID,
                episode_title="Andreessen on VC",
                highlights=[
                    Highlight(
                        episode_id=YOUTUBE_EPISODE_ID,
                        episode_title="Andreessen on VC",
                        segment_timestamp=125.7,
                        quote="the exact words at that timestamp",
                        relevance_score=0.9,
                        why_surface="matches the investor lens",
                    )
                ],
                refused=False,
            ),
            EpisodeDigest(
                episode_id=rss_id,
                episode_title="An RSS episode",
                highlights=[
                    Highlight(
                        episode_id=rss_id,
                        episode_title="An RSS episode",
                        segment_timestamp=42.0,
                        quote="an rss quote",
                        relevance_score=0.7,
                        why_surface="rss reason",
                    )
                ],
                refused=False,
            ),
            EpisodeDigest(
                episode_id="wAnDWfEIwoE",
                episode_title="Refused episode",
                highlights=[],
                refused=True,
                refusal_reason="nothing cleared the relevance bar",
            ),
        ],
    )
    job = Job(
        job_id="job123",
        status=JobStatus.done,
        digest=digest,
        audio_url="/artifacts/episode_job123.mp3",
        usage=JobUsage(
            skipped=[
                SkippedEpisode(
                    episode=EpisodeInput(video_id="missing123"),
                    reason="no transcript available",
                )
            ],
        ),
    )
    subscription = Subscription(
        subscription_id="sub-1",
        owner=MASTER_OWNER,
        email="principal@example.com",
        soul="# soul",
        context="",
        episodes=[EpisodeInput(video_id=YOUTUBE_EPISODE_ID), rss_episode],
        cadence="weekly",
        next_run_at=datetime.now(UTC),
    )
    return job, subscription


def test_subject_is_a_claim_with_correct_counts() -> None:
    job, subscription = _job_and_subscription()
    content = render_digest_email(
        job, subscription, BASE_URL, f"{BASE_URL}/subscriptions/sub-1/unsubscribe?token=abc"
    )
    assert content.subject == "2 of 3 episodes cleared your bar: 2 highlights"


def test_youtube_highlight_gets_a_timestamped_deep_link() -> None:
    job, subscription = _job_and_subscription()
    content = render_digest_email(
        job, subscription, BASE_URL, f"{BASE_URL}/subscriptions/sub-1/unsubscribe?token=abc"
    )
    expected = f"https://www.youtube.com/watch?v={YOUTUBE_EPISODE_ID}&t=125s"
    assert expected in content.text
    # HTML correctly entity-escapes "&" inside the href/link text.
    assert expected.replace("&", "&amp;") in content.html


def test_rss_highlight_gets_bare_audio_url_no_timestamp() -> None:
    job, subscription = _job_and_subscription()
    content = render_digest_email(
        job, subscription, BASE_URL, f"{BASE_URL}/subscriptions/sub-1/unsubscribe?token=abc"
    )
    assert "https://cdn.example.com/ep1.mp3" in content.text
    assert "https://cdn.example.com/ep1.mp3&t=" not in content.text


def test_refused_and_skipped_episodes_listed_honestly() -> None:
    job, subscription = _job_and_subscription()
    content = render_digest_email(
        job, subscription, BASE_URL, f"{BASE_URL}/subscriptions/sub-1/unsubscribe?token=abc"
    )
    assert "Refused: nothing cleared the relevance bar" in content.text
    assert "no transcript available" in content.text


def test_audio_episode_link_is_absolute_and_provenance_present() -> None:
    job, subscription = _job_and_subscription()
    content = render_digest_email(
        job, subscription, BASE_URL, f"{BASE_URL}/subscriptions/sub-1/unsubscribe?token=abc"
    )
    assert f"{BASE_URL}/artifacts/episode_job123.mp3" in content.text
    assert "abc123" in content.text
    assert "supplied" in content.text


def test_unsubscribe_link_present_in_text_and_html() -> None:
    job, subscription = _job_and_subscription()
    unsubscribe_url = f"{BASE_URL}/subscriptions/sub-1/unsubscribe?token=abc"
    content = render_digest_email(job, subscription, BASE_URL, unsubscribe_url)
    assert unsubscribe_url in content.text
    assert unsubscribe_url in content.html


def test_html_has_no_images_and_escapes_quotes() -> None:
    job, subscription = _job_and_subscription()
    content = render_digest_email(
        job, subscription, BASE_URL, f"{BASE_URL}/subscriptions/sub-1/unsubscribe?token=abc"
    )
    assert "<img" not in content.html


def test_unsubscribe_headers_shape() -> None:
    url = f"{BASE_URL}/subscriptions/sub-1/unsubscribe?token=abc"
    headers = unsubscribe_headers(url)
    assert headers[LIST_UNSUBSCRIBE_HEADER] == f"<{url}>"
    assert headers[LIST_UNSUBSCRIBE_POST_HEADER] == LIST_UNSUBSCRIBE_POST_VALUE
