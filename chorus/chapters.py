"""Chapters: the episode's outline written into the MP3, with a link back to
each source moment.

Every spoken line already knows which outline segment it belongs to
(`Turn.segment_index`) and which highlight it rests on (`Turn.citations`).
This module turns that into chapters a podcast player shows, so the grounding
that lives in the JSON is visible while listening.

Start times are estimated: a chapter starts at its section's share of the
spoken characters before it, times the episode's duration. Text-to-speech
reads at a near-constant rate within one episode, so this lands within a few
seconds, which is close enough to navigate by. The source links are exact.

Everything here is pure (bytes in, bytes out):

- `mp3_duration_seconds` walks MPEG audio frame headers and sums their
  durations, with no decoding library.
- `build_chapters` groups the script's lines into chapters: by outline
  segment when the script was written against one, otherwise by runs of
  lines about the same source (host-mode scripts have no outline).
- `source_url` and `source_link` give a source's address and a link that
  opens it at a moment, for the hosts whose timestamp form is documented.
- `write_id3_chapters` replaces any leading ID3v2 tag with an ID3v2.4 tag
  holding a title, one table of contents (`CTOC`) and a `CHAP` frame per
  chapter, each with its own title and link (`WXXX`).
- `chapters_json` renders the Podcasting 2.0 JSON chapters document.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from chorus.models import Chapter, EpisodeDigest, EpisodeInput, Script
from chorus.render_plan import _strip_id3

# --- MPEG audio frame headers ----------------------------------------------

_SYNC_MASK = 0xFFE0
_VERSION_MPEG1 = 3
_VERSION_MPEG2 = 2
_VERSION_MPEG25 = 0
_LAYER_I = 3
_LAYER_II = 2
_LAYER_III = 1
_FRAME_HEADER_BYTES = 4
_ID3V1_MAGIC = b"TAG"

# Bitrates in kbit/s, indexed by the header's 4-bit bitrate index. Index 0
# ("free") and 15 ("bad") are not walkable and stop the walk.
_BITRATES_MPEG1 = {
    _LAYER_I: (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    _LAYER_II: (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    _LAYER_III: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
}
_BITRATES_MPEG2 = {
    _LAYER_I: (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    _LAYER_II: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    _LAYER_III: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_SAMPLE_RATES = {
    _VERSION_MPEG1: (44_100, 48_000, 32_000),
    _VERSION_MPEG2: (22_050, 24_000, 16_000),
    _VERSION_MPEG25: (11_025, 12_000, 8_000),
}


@dataclass(frozen=True)
class _Frame:
    length: int
    seconds: float


def _parse_frame_header(header: bytes) -> _Frame | None:
    """One 4-byte MPEG audio frame header, or None if it is not a valid one."""
    if len(header) < _FRAME_HEADER_BYTES:
        return None
    word = int.from_bytes(header[:_FRAME_HEADER_BYTES], "big")
    if (word >> 16) & _SYNC_MASK != _SYNC_MASK:
        return None
    version = (word >> 19) & 0b11
    layer = (word >> 17) & 0b11
    bitrate_index = (word >> 12) & 0b1111
    rate_index = (word >> 10) & 0b11
    padding = (word >> 9) & 0b1
    if version not in _SAMPLE_RATES or layer == 0 or rate_index == 3:
        return None
    if bitrate_index in (0, 15):
        return None
    table = _BITRATES_MPEG1 if version == _VERSION_MPEG1 else _BITRATES_MPEG2
    bitrate = table[layer][bitrate_index] * 1000
    sample_rate = _SAMPLE_RATES[version][rate_index]
    if layer == _LAYER_I:
        samples = 384
        length = (12 * bitrate // sample_rate + padding) * 4
    elif layer == _LAYER_II or version == _VERSION_MPEG1:
        samples = 1152
        length = 144 * bitrate // sample_rate + padding
    else:  # Layer III, MPEG-2 or 2.5
        samples = 576
        length = 72 * bitrate // sample_rate + padding
    if length < _FRAME_HEADER_BYTES:
        return None
    return _Frame(length=length, seconds=samples / sample_rate)


def mp3_duration_seconds(data: bytes) -> float:
    """Sum of every frame's duration. Skips a leading ID3v2 tag, resyncs a
    byte at a time past junk between frames, and stops at a trailing ID3v1
    tag or the end of the data. Joined MP3s (render chunks byte-joined, see
    chorus.render_plan.join_mp3) walk the same way, because each frame
    decodes on its own."""
    body = _strip_id3(data)
    offset = 0
    total = 0.0
    end = len(body)
    while offset + _FRAME_HEADER_BYTES <= end:
        if body[offset : offset + 3] == _ID3V1_MAGIC and end - offset == 128:
            break
        frame = _parse_frame_header(body[offset : offset + _FRAME_HEADER_BYTES])
        if frame is None or offset + frame.length > end:
            offset += 1
            continue
        total += frame.seconds
        offset += frame.length
    return total


# --- Source links ----------------------------------------------------------

_YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"})
_SPOTIFY_HOSTS = frozenset({"open.spotify.com"})
_AUDIO_SUFFIXES = (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav")
YOUTUBE_WATCH_URL = "https://www.youtube.com/watch?v={video_id}"


def source_url(episode: EpisodeInput) -> str | None:
    """Where a listener can open the source: the YouTube watch page for a
    YouTube episode, or the audio file for an RSS episode. An RSS episode's
    feed alone is not a place to listen, so it gives None."""
    if episode.url:
        return episode.url
    if episode.video_id:
        return YOUTUBE_WATCH_URL.format(video_id=episode.video_id)
    return episode.audio_url


def _with_query(url: str, key: str, value: str) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != key]
    query.append((key, value))
    return urlunsplit(parts._replace(query=urlencode(query)))


def source_link(url: str | None, seconds: float | None) -> str | None:
    """A link that opens `url` at `seconds`, in the form each host documents:
    YouTube `t=<n>s`, Spotify `t=<n>`, and the W3C media fragment `#t=<n>`
    for a direct audio file. Any other host (Apple Podcasts episode pages
    included) gets the URL unchanged rather than a guessed parameter."""
    if not url:
        return None
    if seconds is None:
        return url
    at = max(0, int(seconds))
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if host in _YOUTUBE_HOSTS:
        return _with_query(url, "t", f"{at}s")
    if host in _SPOTIFY_HOSTS:
        return _with_query(url, "t", str(at))
    if parts.path.lower().endswith(_AUDIO_SUFFIXES):
        return urlunsplit(parts._replace(fragment=f"t={at}"))
    return url


# --- Building chapters -----------------------------------------------------

INTRO_TITLE = "Intro"


@dataclass(frozen=True)
class _Line:
    text: str
    group: str
    title: str
    episode_id: str | None
    timestamp: float | None


def _source_title(digest: EpisodeDigest | None, episode_id: str) -> str:
    if digest is None:
        return episode_id
    title = digest.episode_title or digest.episode_id
    return f"{digest.show}: {title}" if digest.show else title


def _lines(script: Script, digests: dict[str, EpisodeDigest]) -> list[_Line]:
    """The script's spoken lines, each labelled with the chapter it belongs
    to. Outline segments win when every line has one; otherwise a line about
    a source opens (or continues) that source's chapter and an uncited line
    stays in the chapter before it."""
    segments = script.outline.segments if script.outline else []
    turns = script.turns
    if turns and segments and all(
        t.segment_index is not None and 0 <= t.segment_index < len(segments) for t in turns
    ):
        out: list[_Line] = []
        for turn in turns:
            assert turn.segment_index is not None  # checked above; narrows for mypy
            cite = next((c for c in turn.citations if c.segment_timestamp is not None), None)
            out.append(
                _Line(
                    text=turn.text,
                    group=f"segment-{turn.segment_index}",
                    title=segments[turn.segment_index].name,
                    episode_id=cite.episode_id if cite else None,
                    timestamp=cite.segment_timestamp if cite else None,
                )
            )
        return out

    spoken = (
        [(t.text, t.episode_id, t.segment_timestamp) for t in turns]
        if turns
        else [(t.text, t.episode_id, t.segment_timestamp) for t in script.takes]
    )
    out = []
    group, title = "intro", INTRO_TITLE
    for text, episode_id, timestamp in spoken:
        if episode_id and episode_id in digests and group != f"source-{episode_id}":
            group, title = f"source-{episode_id}", _source_title(digests[episode_id], episode_id)
        out.append(
            _Line(
                text=text,
                group=group,
                title=title,
                episode_id=episode_id if episode_id in digests else None,
                timestamp=timestamp if episode_id in digests else None,
            )
        )
    return out


def build_chapters(
    script: Script, digests: list[EpisodeDigest], duration_seconds: float
) -> list[Chapter]:
    """Chapters in broadcast order. Each starts at its share of the spoken
    characters before it, scaled to `duration_seconds`, and links to the
    first source moment its lines cite. A section with no spoken text gets
    no chapter."""
    by_id = {d.episode_id: d for d in digests}
    lines = [line for line in _lines(script, by_id) if line.text.strip()]
    total_chars = sum(len(line.text) for line in lines)
    if not lines or total_chars == 0 or duration_seconds <= 0:
        return []

    chapters: list[Chapter] = []
    spoken_before = 0
    current: str | None = None
    for line in lines:
        if line.group != current:
            current = line.group
            chapters.append(
                Chapter(
                    start_seconds=round(duration_seconds * spoken_before / total_chars, 3),
                    title=line.title,
                )
            )
        chapter = chapters[-1]
        if chapter.episode_id is None and line.episode_id is not None:
            digest = by_id.get(line.episode_id)
            chapter.episode_id = line.episode_id
            chapter.source_timestamp = line.timestamp
            chapter.url = source_link(digest.url if digest else None, line.timestamp)
        spoken_before += len(line.text)
    return chapters[:MAX_CHAPTERS]


# --- ID3v2.4 -----------------------------------------------------------------

MAX_CHAPTERS = 255  # CTOC stores its entry count in one byte
_ID3_VERSION = b"\x04\x00"
_ENCODING_UTF8 = b"\x03"
_NO_OFFSET = b"\xff\xff\xff\xff"
_TOC_ID = b"toc"
_CTOC_TOP_LEVEL_ORDERED = b"\x03"
_SYNCHSAFE_MAX = (1 << 28) - 1


def _synchsafe(n: int) -> bytes:
    if not 0 <= n <= _SYNCHSAFE_MAX:
        raise ValueError(f"ID3 size {n} does not fit a synchsafe integer")
    return bytes(((n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F))


def _frame(frame_id: bytes, body: bytes) -> bytes:
    return frame_id + _synchsafe(len(body)) + b"\x00\x00" + body


def _text_frame(frame_id: bytes, text: str) -> bytes:
    return _frame(frame_id, _ENCODING_UTF8 + text.encode("utf-8"))


def _url_frame(url: str) -> bytes:
    # WXXX: encoding, empty description (terminated), then the URL in ISO-8859-1.
    return _frame(b"WXXX", _ENCODING_UTF8 + b"\x00" + url.encode("latin-1", "replace"))


def _ms(seconds: float) -> bytes:
    return int(round(seconds * 1000)).to_bytes(4, "big")


def id3_chapter_tag(chapters: list[Chapter], duration_seconds: float, title: str) -> bytes:
    """A complete ID3v2.4 tag: `TIT2`, a top-level ordered `CTOC`, and one
    `CHAP` per chapter (start/end in milliseconds, byte offsets unset) with
    its title and, when known, the source link."""
    chapters = chapters[:MAX_CHAPTERS]
    element_ids = [f"ch{i}".encode("ascii") for i in range(len(chapters))]
    frames = [_text_frame(b"TIT2", title)]
    frames.append(
        _frame(
            b"CTOC",
            _TOC_ID + b"\x00" + _CTOC_TOP_LEVEL_ORDERED + bytes([len(chapters)])
            + b"".join(e + b"\x00" for e in element_ids),
        )
    )
    for i, (element_id, chapter) in enumerate(zip(element_ids, chapters, strict=True)):
        end = chapters[i + 1].start_seconds if i + 1 < len(chapters) else duration_seconds
        sub = _text_frame(b"TIT2", chapter.title)
        if chapter.url:
            sub += _url_frame(chapter.url)
        frames.append(
            _frame(
                b"CHAP",
                element_id + b"\x00" + _ms(chapter.start_seconds) + _ms(end)
                + _NO_OFFSET + _NO_OFFSET + sub,
            )
        )
    body = b"".join(frames)
    return b"ID3" + _ID3_VERSION + b"\x00" + _synchsafe(len(body)) + body


def write_id3_chapters(
    data: bytes, chapters: list[Chapter], duration_seconds: float, title: str
) -> bytes:
    """`data` with any leading ID3v2 tag replaced by the chapter tag."""
    return id3_chapter_tag(chapters, duration_seconds, title) + _strip_id3(data)


# --- Podcasting 2.0 JSON chapters -------------------------------------------

JSON_CHAPTERS_VERSION = "1.2.0"


def chapters_json(chapters: list[Chapter]) -> str:
    """The `podcast:chapters` document (application/json+chapters)."""
    doc = {
        "version": JSON_CHAPTERS_VERSION,
        "chapters": [
            {"startTime": c.start_seconds, "title": c.title, **({"url": c.url} if c.url else {})}
            for c in chapters
        ],
    }
    return json.dumps(doc, ensure_ascii=False)


# --- The one call the audio paths make -------------------------------------

EPISODE_TITLE = "Chorus"


@dataclass(frozen=True)
class TaggedAudio:
    data: bytes
    chapters: list[Chapter]
    duration_seconds: float


def tag_episode(
    data: bytes, script: Script, digests: list[EpisodeDigest], title: str = EPISODE_TITLE
) -> TaggedAudio:
    """Measure the episode, build its chapters and write them into the MP3.
    Audio with no walkable frames, or a script with nothing spoken, comes
    back unchanged with no chapters."""
    duration = mp3_duration_seconds(data)
    chapters = build_chapters(script, digests, duration)
    if not chapters:
        return TaggedAudio(data=data, chapters=[], duration_seconds=duration)
    return TaggedAudio(
        data=write_id3_chapters(data, chapters, duration, title),
        chapters=chapters,
        duration_seconds=duration,
    )
