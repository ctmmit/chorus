"""The "host-plugin" voice: the principal's agent voices the episode.

Chorus cannot call the agent's text-to-speech tool itself (an ElevenLabs MCP
server, connector, or plugin living in the host). So it turns the finished
script into a render plan: ordered chunks, each with the text, the speaker
role and a voice id. The agent voices each chunk with its own tool and hands
the files back; Chorus checks they are MP3 and joins them in order.

Chunking mirrors the built-in renderers (`chorus.audio`): a dialogue becomes
one chunk per turn (consecutive lines by the same speaker merged), and any
text over `MAX_CHUNK_CHARS` splits on sentence boundaries with
`chorus.audio._split_long_turn`. The official ElevenLabs MCP server has no
dialogue tool, so two hosts are voiced turn by turn, which loses the
cross-turn prosody of Chorus's own Text-to-Dialogue renderer.

MP3 frames decode independently, so byte-joining complete files plays back
correctly (the same approach `ElevenLabsDialogueRenderer` uses). A leading
ID3v2 tag is stripped from every chunk after the first so tag bytes never land
mid-stream.
"""
from __future__ import annotations

import base64
import binascii
from pathlib import Path

from pydantic import BaseModel

from chorus.audio import (
    DEFAULT_COHOST_VOICE_ID,
    DEFAULT_MODEL,
    DEFAULT_VOICE_ID,
    DIALOGUE_MAX_CHARS,
    OUTPUT_FORMAT,
    _split_long_turn,
)
from chorus.models import Script, Turn

MAX_CHUNK_CHARS = DIALOGUE_MAX_CHARS
MAX_CHUNK_BYTES = 25 * 1024 * 1024
MAX_EPISODE_BYTES = 150 * 1024 * 1024
ID3_HEADER_BYTES = 10
_ID3_MAGIC = b"ID3"
_FRAME_SYNC_MASK = 0xE0
_DEFAULT_VOICES = {"host": DEFAULT_VOICE_ID, "cohost": DEFAULT_COHOST_VOICE_ID}


class RenderChunk(BaseModel):
    index: int
    role: str
    voice_id: str
    text: str


class RenderPlan(BaseModel):
    format: str
    model_id: str = DEFAULT_MODEL
    output_format: str = OUTPUT_FORMAT
    chunks: list[RenderChunk]


class AudioChunkError(ValueError):
    """A submitted chunk that is missing, too large, or not MP3."""


def _voice(script: Script, role: str, env_voices: dict[str, str]) -> str:
    return script.voices.get(role) or env_voices.get(role) or _DEFAULT_VOICES[role]


def build_plan(script: Script, env_voices: dict[str, str] | None = None) -> RenderPlan:
    """`env_voices` carries the principal's ELEVENLABS_VOICE_ID /
    ELEVENLABS_COHOST_VOICE_ID overrides; a profile's per-speaker voice on the
    script wins over both, exactly as in `chorus.audio`."""
    voices = env_voices or {}
    if script.format == "dialogue" and script.turns:
        merged: list[Turn] = []
        for turn in script.turns:
            last = merged[-1] if merged else None
            if last is not None and last.speaker == turn.speaker:
                merged[-1] = last.model_copy(update={"text": f"{last.text} {turn.text}"})
            else:
                merged.append(turn)
        lines = [p for t in merged for p in _split_long_turn(t, MAX_CHUNK_CHARS)]
    else:
        # A monologue is one long "turn" by the host; reuse the same splitter.
        whole = Turn(speaker="host", text=script.monologue, episode_id="-", segment_timestamp=0)
        lines = _split_long_turn(whole, MAX_CHUNK_CHARS)
    chunks = [
        RenderChunk(index=i, role=t.speaker, voice_id=_voice(script, t.speaker, voices), text=t.text)
        for i, t in enumerate(lines)
        if t.text.strip()
    ]
    return RenderPlan(format=script.format, chunks=chunks)


def _strip_id3(data: bytes) -> bytes:
    """Drop a leading ID3v2 tag. Its size is a 28-bit synchsafe integer in
    header bytes 6-9, plus a 10-byte footer when flag bit 4 is set."""
    if not data.startswith(_ID3_MAGIC) or len(data) < ID3_HEADER_BYTES:
        return data
    size = 0
    for byte in data[6:10]:
        size = (size << 7) | (byte & 0x7F)
    footer = ID3_HEADER_BYTES if data[5] & 0x10 else 0
    return data[ID3_HEADER_BYTES + size + footer :]


def is_mp3(data: bytes) -> bool:
    body = _strip_id3(data)
    return len(body) >= 2 and body[0] == 0xFF and (body[1] & _FRAME_SYNC_MASK) == _FRAME_SYNC_MASK


def read_chunk(path: str | None = None, data_base64: str | None = None) -> bytes:
    """One chunk from a file path (what the ElevenLabs MCP server returns) or
    base64 bytes (hosts that only receive audio as an MCP resource)."""
    if (path is None) == (data_base64 is None):
        raise AudioChunkError("give exactly one of path or base64")
    if path is not None:
        file = Path(path).expanduser()
        if not file.is_file():
            raise AudioChunkError(f"no file at {file}")
        if file.stat().st_size > MAX_CHUNK_BYTES:
            raise AudioChunkError(f"{file} is larger than {MAX_CHUNK_BYTES} bytes")
        data = file.read_bytes()
    else:
        try:
            data = base64.b64decode(data_base64 or "", validate=True)
        except binascii.Error as err:
            raise AudioChunkError(f"invalid base64: {err}") from err
        if len(data) > MAX_CHUNK_BYTES:
            raise AudioChunkError(f"chunk is larger than {MAX_CHUNK_BYTES} bytes")
    if not is_mp3(data):
        raise AudioChunkError("not an MP3 file (ask your voice tool for mp3 output)")
    return data


def join_mp3(parts: list[bytes]) -> bytes:
    if not parts:
        raise AudioChunkError("no audio chunks")
    joined = parts[0] + b"".join(_strip_id3(p) for p in parts[1:])
    if len(joined) > MAX_EPISODE_BYTES:
        raise AudioChunkError(f"episode is larger than {MAX_EPISODE_BYTES} bytes")
    return joined
