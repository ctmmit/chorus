"""Digest email rendering (Phase F, docs/DEVELOPMENT_PLAN.md §3): pure
functions from a finished `Job` + `Subscription` to an `EmailContent` — no
network, no state, so tests/test_digest_email.py can golden-check them without
mocking anything. `chorus/scheduler.py` is the only caller in production.

Two templates: the digest itself (highlights grouped by source title) and the
short "nothing new" note a feed subscription sends on a week with no new
episodes. Both end with the same honest footer: what could not be checked,
and the unsubscribe link.

Plain, text-first, Fulcrum-style restraint: the HTML body is the same
content as the plain text, lightly marked up, no images, no color beyond
navy headings, system fonts only.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from html import escape
from urllib.parse import urljoin

from chorus.curation import REFUSAL
from chorus.feedback import rating_link
from chorus.models import EpisodeDigest, EpisodeInput, Highlight, Job
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
EMPTY_WEEK_SUBJECT = "Nothing new from your shows this week"
EMPTY_DAY_SUBJECT = "Nothing new from your shows today"
ALL_SOURCES_FAILED_SUBJECT = "Could not check your shows this week"
OTHER_EPISODES_LABEL = "Other episodes"


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


def _episode_lookup(
    subscription: Subscription, episodes: Sequence[EpisodeInput] | None = None
) -> dict[str, EpisodeInput]:
    lookup = {e.resolved_id(): e for e in (subscription.episodes or [])}
    for episode in episodes or ():
        lookup[episode.resolved_id()] = episode
    return lookup


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


def _group_by_source(
    episodes: Sequence[EpisodeDigest], lookup: dict[str, EpisodeInput]
) -> list[tuple[str | None, list[EpisodeDigest]]]:
    """Episodes grouped by the source (show/channel) title, in order of first
    appearance. When no episode has a known source title the result is one
    unlabelled group, which renders exactly like the pre-feed layout."""
    groups: dict[str | None, list[EpisodeDigest]] = {}
    for ep in episodes:
        source = lookup.get(ep.episode_id)
        groups.setdefault(source.show if source is not None else None, []).append(ep)
    if set(groups) == {None}:
        return [(None, groups[None])]
    ordered: list[tuple[str | None, list[EpisodeDigest]]] = [
        (label, eps) for label, eps in groups.items() if label is not None
    ]
    if None in groups:
        ordered.append((OTHER_EPISODES_LABEL, groups[None]))
    return ordered


def _footer_notes(
    feed_errors: Sequence[str], not_included: int, max_episodes_per_run: int | None
) -> tuple[list[str], list[str]]:
    """(text lines, html fragments) for the honest-footer notes."""
    text: list[str] = []
    html: list[str] = []
    if not_included > 0:
        cap = f" (cap of {max_episodes_per_run} per run)" if max_episodes_per_run else ""
        word = "episode was" if not_included == 1 else "episodes were"
        line = f"{not_included} more new {word} not included{cap}."
        text.append(line)
        html.append(f'<p style="color:#5b6472;">{escape(line)}</p>')
    if feed_errors:
        text.append("Could not check:")
        html.append(f'<h2 style="color:{NAVY}; font-size:16px;">Could not check</h2><ul>')
        for line in feed_errors:
            text.append(f"- {line}")
            html.append(f"<li>{escape(line)}</li>")
        html.append("</ul>")
    if text:
        text.append("")
    return text, html


def _wrap_html(subject: str, sections: list[str]) -> str:
    return (
        f'<div style="font-family:{SYSTEM_FONT_STACK}; color:#1C2434; line-height:1.5;">'
        f'<h1 style="color:{NAVY}; font-size:20px; margin:0 0 12px;">{escape(subject)}</h1>'
        + "".join(sections)
        + "</div>"
    )


def render_digest_email(
    job: Job,
    subscription: Subscription,
    base_url: str,
    unsubscribe_url: str,
    *,
    episodes: Sequence[EpisodeInput] | None = None,
    feed_errors: Sequence[str] = (),
    not_included: int = 0,
) -> EmailContent:
    """`job` must be a `done` job with `job.digest` set — callers (chorus.
    scheduler.run_subscription) only reach this branch on success; a failed
    job gets its own short failure email instead.

    `episodes` are the run's episodes for a feed subscription (they are not
    stored on the subscription): they supply the source title used for
    grouping and the audio link for RSS highlights. `feed_errors` and
    `not_included` feed the footer."""
    digest = job.digest
    assert digest is not None
    subject = _subject(job)
    episode_lookup = _episode_lookup(subscription, episodes)
    groups = _group_by_source(digest.episodes, episode_lookup)
    grouped = groups[0][0] is not None

    text_lines: list[str] = [subject, ""]
    html_sections: list[str] = []

    # Questions two or more sources spoke to this week (chorus/threads.py),
    # above the per-source highlights: that conversation is the headline.
    if digest.threads:
        by_id = {h.highlight_id: h for h in digest.highlights}
        text_lines.append("# This week's threads")
        thread_html = [f'<h2 style="color:{NAVY}; font-size:17px; margin:16px 0 2px;">'
                       "This week's threads</h2>"]
        for thread in digest.threads:
            text_lines.append(f"## {thread.question}")
            items: list[str] = []
            for member in thread.members:
                h = by_id.get(member.highlight_id)
                if h is None:
                    continue
                source = h.show or h.episode_title or h.episode_id
                text_lines.append(f"- {source} ({member.stance}): {h.quote}")
                items.append(
                    f"<li>{escape(source)} <em>({escape(member.stance)})</em>: "
                    f"“{escape(h.quote)}”</li>"
                )
            text_lines.append("")
            thread_html.append(
                f'<p style="margin:10px 0 2px;"><strong>{escape(thread.question)}</strong></p>'
                f'<ul style="margin:0 0 8px;">{"".join(items)}</ul>'
            )
        html_sections.append("".join(thread_html))

    for label, group_episodes in groups:
        if label is not None:
            text_lines.append(f"# {label}")
            html_sections.append(
                f'<h2 style="color:{NAVY}; font-size:17px; margin:24px 0 2px;">{escape(label)}</h2>'
            )
        heading_tag, heading_size = ("h3", "15px") if grouped else ("h2", "16px")
        for ep in group_episodes:
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
                    more = rating_link(base_url, job, h.highlight_id, "up")
                    less = rating_link(base_url, job, h.highlight_id, "down")
                    text_lines.append(f"- {h.quote}")
                    text_lines.append(f"  Why: {h.why_surface}")
                    if link:
                        text_lines.append(f"  Listen: {link}")
                    text_lines.append(f"  More like this: {more}")
                    text_lines.append(f"  Less like this: {less}")
                    html_body.append(
                        '<p style="margin:0 0 4px;">“' + escape(h.quote) + "”<br>"
                        f'<span style="color:#5b6472;">{escape(h.why_surface)}</span>'
                        + (f'<br><a href="{escape(link)}">{escape(link)}</a>' if link else "")
                        + '<br><span style="font-size:12px;">'
                        f'<a href="{escape(more)}">More like this</a> · '
                        f'<a href="{escape(less)}">Less like this</a></span>'
                        + "</p>"
                    )
            text_lines.append("")
            html_sections.append(
                f'<{heading_tag} style="color:{NAVY}; font-size:{heading_size}; '
                f'margin:20px 0 6px;">{escape(title)}</{heading_tag}>' + "".join(html_body)
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

    note_text, note_html = _footer_notes(
        feed_errors, not_included, subscription.max_episodes_per_run if subscription.sources else None
    )
    text_lines.extend(note_text)
    html_sections.extend(note_html)

    provenance = f"Lens: soul {digest.soul_version} ({digest.soul_origin})"
    text_lines.append(provenance)
    text_lines.append("")
    text_lines.append(f"Unsubscribe: {unsubscribe_url}")
    html_sections.append(
        f'<p style="color:#8B9199; font-size:12px;">{escape(provenance)}</p>'
        f'<p style="color:#8B9199; font-size:12px;">'
        f'<a href="{escape(unsubscribe_url)}">Unsubscribe</a></p>'
    )

    return EmailContent(
        subject=subject, text="\n".join(text_lines), html=_wrap_html(subject, html_sections)
    )


def render_empty_email(
    subscription: Subscription,
    unsubscribe_url: str,
    *,
    sources_checked: Sequence[str],
    since: datetime,
    feed_errors: Sequence[str] = (),
) -> EmailContent:
    """The short note a feed subscription sends when a run finds nothing new.
    When every source failed to load, the subject says so instead of claiming
    there was nothing to find."""
    all_failed = bool(feed_errors) and len(feed_errors) >= len(sources_checked)
    if all_failed:
        subject = ALL_SOURCES_FAILED_SUBJECT
    else:
        subject = EMPTY_DAY_SUBJECT if subscription.cadence == "daily" else EMPTY_WEEK_SUBJECT
    since_label = since.strftime("%d %b %Y")

    text_lines = [subject, ""]
    html_sections: list[str] = []
    if all_failed:
        intro = "None of your sources could be read, so nothing was checked."
    else:
        intro = f"No new episodes were published since {since_label}."
    text_lines.append(intro)
    text_lines.append("")
    html_sections.append(f"<p>{escape(intro)}</p>")

    text_lines.append("Sources checked:")
    html_sections.append(f'<h2 style="color:{NAVY}; font-size:16px;">Sources checked</h2><ul>')
    for label in sources_checked:
        text_lines.append(f"- {label}")
        html_sections.append(f"<li>{escape(label)}</li>")
    html_sections.append("</ul>")
    text_lines.append("")

    note_text, note_html = _footer_notes(feed_errors, 0, None)
    text_lines.extend(note_text)
    html_sections.extend(note_html)

    text_lines.append(f"Unsubscribe: {unsubscribe_url}")
    html_sections.append(
        f'<p style="color:#8B9199; font-size:12px;">'
        f'<a href="{escape(unsubscribe_url)}">Unsubscribe</a></p>'
    )
    return EmailContent(
        subject=subject, text="\n".join(text_lines), html=_wrap_html(subject, html_sections)
    )
