"""Digest email rendering (Phase F, docs/DEVELOPMENT_PLAN.md §3): a pure
function from a finished `Job` + `Subscription` to an `EmailContent` — no
network, no state, so tests/test_digest_email.py can golden-check it without
mocking anything. `chorus/scheduler.py` is the only caller in production.

Plain, text-first, Fulcrum-style restraint: the HTML body is the same
content as the plain text, lightly marked up, no images, no color beyond
navy headings, system fonts only.
"""
from __future__ import annotations

from dataclasses import dataclass
from html import escape
from urllib.parse import urljoin

from chorus.curation import REFUSAL
from chorus.models import EpisodeInput, Highlight, Job
from chorus.subscriptions import Subscription

# The one non-text color this template uses, for headings only.
NAVY = "#0F2340"
SYSTEM_FONT_STACK = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
YOUTUBE_WATCH_URL = "https://www.youtube.com/watch"
# EpisodeInput.resolved_id() prefixes a deterministic id with this for any
# episode identified by RSS guid/audio_url rather than a YouTube id.
RSS_EPISODE_ID_PREFIX = "rss-"
LIST_UNSUBSCRIBE_HEADER = "List-Unsubscribe"
LIST_UNSUBSCRIBE_POST_HEADER = "List-Unsubscribe-Post"
LIST_UNSUBSCRIBE_POST_VALUE = "List-Unsubscribe=One-Click"


@dataclass(frozen=True)
class EmailContent:
    subject: str
    text: str
    html: str


def unsubscribe_headers(unsubscribe_url: str) -> dict[str, str]:
    """RFC 8058 one-click unsubscribe headers Gmail/Yahoo require of bulk
    senders. Pass to EmailSender.send(..., headers=...)."""
    return {
        LIST_UNSUBSCRIBE_HEADER: f"<{unsubscribe_url}>",
        LIST_UNSUBSCRIBE_POST_HEADER: LIST_UNSUBSCRIBE_POST_VALUE,
    }


def _episode_lookup(subscription: Subscription) -> dict[str, EpisodeInput]:
    return {e.resolved_id(): e for e in (subscription.episodes or [])}


def _highlight_link(highlight: Highlight, episode_lookup: dict[str, EpisodeInput]) -> str | None:
    """YouTube episodes get a per-highlight deep link at the cited second.
    RSS episodes (identified by guid/audio_url, never a real timestamped
    web player) get the plain audio_url with no timestamp."""
    if highlight.episode_id.startswith(RSS_EPISODE_ID_PREFIX):
        episode = episode_lookup.get(highlight.episode_id)
        return episode.audio_url if episode is not None else None
    seconds = int(highlight.segment_timestamp)
    return f"{YOUTUBE_WATCH_URL}?v={highlight.episode_id}&t={seconds}s"


def _absolute(base_url: str, url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith(("http://", "https://")):
        return url
    return urljoin(base_url, url)


def _subject(job: Job) -> str:
    digest = job.digest
    assert digest is not None  # caller only renders a digest email for a done job
    episodes = digest.episodes
    cleared = sum(1 for ep in episodes if not ep.refused and ep.highlights)
    total_highlights = sum(len(ep.highlights) for ep in episodes)
    ep_word = "episode" if len(episodes) == 1 else "episodes"
    hl_word = "highlight" if total_highlights == 1 else "highlights"
    return f"{cleared} of {len(episodes)} {ep_word} cleared your bar: {total_highlights} {hl_word}"


def _episode_label(episode: EpisodeInput) -> str:
    return episode.title or episode.show or episode.video_id or episode.resolved_id()


def render_digest_email(
    job: Job, subscription: Subscription, base_url: str, unsubscribe_url: str
) -> EmailContent:
    """`job` must be a `done` job with `job.digest` set — callers (chorus.
    scheduler.run_subscription) only reach this branch on success; a failed
    job gets its own short failure email instead."""
    digest = job.digest
    assert digest is not None
    subject = _subject(job)
    episode_lookup = _episode_lookup(subscription)

    text_lines: list[str] = [subject, ""]
    html_sections: list[str] = []

    for ep in digest.episodes:
        title = ep.episode_title or ep.episode_id
        text_lines.append(f"## {title}")
        html_body: list[str] = []
        if ep.refused or not ep.highlights:
            reason = ep.refusal_reason or REFUSAL
            text_lines.append(f"  Refused: {reason}")
            html_body.append(f'<p style="color:#5b6472;">Refused: {escape(reason)}</p>')
        else:
            for h in ep.highlights:
                link = _highlight_link(h, episode_lookup)
                text_lines.append(f"- {h.quote}")
                text_lines.append(f"  Why: {h.why_surface}")
                if link:
                    text_lines.append(f"  Listen: {link}")
                html_body.append(
                    '<p style="margin:0 0 4px;">“' + escape(h.quote) + "”<br>"
                    f'<span style="color:#5b6472;">{escape(h.why_surface)}</span>'
                    + (
                        f'<br><a href="{escape(link)}">{escape(link)}</a>'
                        if link
                        else ""
                    )
                    + "</p>"
                )
        text_lines.append("")
        html_sections.append(
            f'<h2 style="color:{NAVY}; font-size:16px; margin:20px 0 6px;">{escape(title)}</h2>'
            + "".join(html_body)
        )

    skipped = job.usage.skipped if job.usage else []
    if skipped:
        text_lines.append("Skipped episodes:")
        skipped_html = [f'<h2 style="color:{NAVY}; font-size:16px;">Skipped episodes</h2>', "<ul>"]
        for s in skipped:
            label = _episode_label(s.episode)
            text_lines.append(f"- {label}: {s.reason}")
            skipped_html.append(f"<li>{escape(label)}: {escape(s.reason)}</li>")
        skipped_html.append("</ul>")
        text_lines.append("")
        html_sections.append("".join(skipped_html))

    audio_link = _absolute(base_url, job.audio_url)
    if audio_link:
        text_lines.append(f"Full audio episode: {audio_link}")
        text_lines.append("")
        html_sections.append(
            f'<p><a href="{escape(audio_link)}">Listen to the full audio episode</a></p>'
        )

    provenance = f"Lens: soul {digest.soul_version} ({digest.soul_origin})"
    text_lines.append(provenance)
    text_lines.append("")
    text_lines.append(f"Unsubscribe: {unsubscribe_url}")
    html_sections.append(
        f'<p style="color:#8B9199; font-size:12px;">{escape(provenance)}</p>'
        f'<p style="color:#8B9199; font-size:12px;">'
        f'<a href="{escape(unsubscribe_url)}">Unsubscribe</a></p>'
    )

    text = "\n".join(text_lines)
    html = (
        f'<div style="font-family:{SYSTEM_FONT_STACK}; color:#1C2434; line-height:1.5;">'
        f'<h1 style="color:{NAVY}; font-size:20px; margin:0 0 12px;">{escape(subject)}</h1>'
        + "".join(html_sections)
        + "</div>"
    )
    return EmailContent(subject=subject, text=text, html=html)
