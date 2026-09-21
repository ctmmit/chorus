---
name: chorus
description: Turn a week of podcast episodes into a persona-conditioned, source-cited highlights digest plus a short audio episode in your own voice. For an autonomous agent acting for a principal who follows more shows than they can hear.
---

# Chorus

You (an agent) hand Chorus your **soul** (your analytical lens), your principal's
**context**, and a list of **episodes**. It reads every transcript, surfaces only
the segments that clear your relevance bar (each with a resolvable timestamp),
writes an opinionated script in your voice, and renders a short audio episode.
This is a thought partner with a point of view, not a generic summary.

## Base URL & auth

- Base URL: the deployed service (e.g. `https://chorus.up.railway.app`).
- Auth: send `Authorization: Bearer <token>` on **every** request, including
  `audio_url` downloads. The token is issued by whoever deployed the service.
  A missing or wrong token returns `401`. (A dev instance running only mock
  providers may have auth disabled; a deployed one with real keys never does.)
- The service holds its own provider keys server-side; you never send them.

## Limits

| field | limit |
|---|---|
| `soul` | 1 to 40,000 characters |
| `context` | up to 40,000 characters |
| `episodes` | 1 to 25 per request |
| `highlight_count` | 1 to 20 |
| request body | 1 MiB |

Out-of-range fields return `422`; an oversized body returns `413`.

## Contract

The job is async (audio takes minutes). One submit, then poll one endpoint.

### 1. Submit

`POST /digest`

```json
{
  "soul": "<markdown: your lens — see 'Building a soul' below>",
  "context": "<plain text: what the principal is working on/reading this week>",
  "episodes": [
    { "video_id": "gs39QFYIbBY" },
    { "url": "https://www.youtube.com/watch?v=c4tvVKDhpiY" },
    { "feed_url": "https://feeds.example.com/show.xml", "guid": "ep-142" }
  ],
  "highlight_count": 4
}
```

Each episode is one of:

- `{ "video_id": "..." }` or `{ "url": "..." }` — a YouTube episode.
- `{ "feed_url": "...", "guid": "..." }` (optionally `"audio_url"` too) — a
  podcast RSS episode, identified the way Podcasting 2.0 identifies it: the
  feed plus the `<guid>` of the `<item>`. Use `audio_url` instead of `guid`
  if that's all you have (the service matches on `<enclosure url>`).

The service resolves a transcript through a provider ladder (managed YouTube
captions, the episode's own RSS `podcast:transcript` tag, then speech-to-text
on the audio as a last resort) — you never need to know which one fired;
`usage.transcript_sources` (below) tells you after the fact if you're curious.
Returns:

```json
{ "job_id": "a1b2c3..." }
```

### 2. Poll

`GET /digest/{job_id}` → a job whose `status` progresses:

| status | meaning | what's present |
|---|---|---|
| `queued` | accepted, not started | — |
| `digest_ready` | text digest done (≤ ~90s) | `digest` |
| `done` | digest complete; script + audio rendered if they could be (≤ ~5 min) | `digest`, `script`, `audio_url`, `warnings` |
| `failed` | could not produce a digest, or the service restarted mid-job | `error` |

Poll until `done` or `failed`. Every job reaches one of those two states; a job
never stays `queued` or `digest_ready` indefinitely. The digest is usable at
`digest_ready` if you don't want to wait for audio.

### Response shape (`done`)

```json
{
  "job_id": "a1b2c3...",
  "status": "done",
  "digest": {
    "soul_version": "1a2b3c4d",
    "episodes": [
      {
        "episode_id": "c4tvVKDhpiY",
        "episode_title": "...",
        "refused": false,
        "highlights": [
          {
            "episode_id": "c4tvVKDhpiY",
            "segment_timestamp": 1800.0,
            "quote": "the exact words at that timestamp...",
            "relevance_score": 0.82,
            "why_surface": "why this cleared your bar"
          }
        ]
      }
    ]
  },
  "script": { "soul_version": "1a2b3c4d", "takes": [ ... ], "monologue": "..." },
  "audio_url": "/artifacts/episode_a1b2c3....mp3",
  "warnings": [],
  "usage": {
    "stage_seconds": { "ingest": 0.8, "curate": 12.4, "script": 3.1, "audio": 9.6 },
    "transcript_sources": { "c4tvVKDhpiY": "supadata", "ep-142": "rss:json" },
    "skipped": [ { "episode": { "...": "..." }, "reason": "why it couldn't resolve" } ]
  }
}
```

- Every highlight's `segment_timestamp` + `quote` resolve to the real transcript.
- `usage` is run telemetry, not part of the lifecycle contract — safe to ignore.
  `transcript_sources` maps each resolved episode id to which provider produced
  its transcript ("fixture", "supadata", "rss:json"/"rss:vtt"/"rss:srt", or
  "deepgram"); `skipped` lists episodes that never resolved and why (the same
  information a skipped episode's absence from `digest.episodes` otherwise
  throws away).
- An episode with nothing relevant comes back `refused: true` with
  `refusal_reason: "nothing cleared the relevance bar"` — never an invented reason.
- `audio_url` is downloadable from the base URL (send the bearer token). It is
  unique per job. It may be `null` if audio rendering failed; `script` may
  likewise be `null` if script synthesis failed. In both cases the digest is
  still valid and `warnings` says what degraded (degrade gracefully).

## Errors

- `401` — missing or invalid bearer token.
- `404` on `GET /digest/{job_id}` — unknown `job_id`.
- `413` / `422` — body too large / a field outside the limits above.
- `status: "failed"` with `error` — e.g. none of the episodes had a retrievable
  transcript, a provider call failed before the digest existed, or the service
  restarted mid-job. Surface the error; do not retry blindly.
- A single episode with no transcript is **skipped**, not fatal — the digest
  completes on the rest.

## Building a soul (source-agnostic)

The `soul` is your lens as markdown. Any of these produces one; the service only
consumes the file, so use whichever you can:

1. **Supplied** — you already hold a persona doc. Send it.
2. **Derived from a corpus** — summarize what the principal reads/saves (Readwise,
   Obsidian, a notes export, even prior digests) into the sections below.
3. **Interview** — if you have neither, ask the principal a few questions and
   write the sections yourself.
4. **Seed** — cold start from a few stated interests.

Recommended sections: **Identity & Role**, **Core Interests**, **Attention
Triggers** (surface a segment when it touches these), **Anti-interests** (ignore),
**Taste & Sensibility**, and **Curation Guidance** (explicit scoring calibration,
e.g. "bar is high for AI content; surface only when genuinely novel"). The last
one most affects what gets surfaced.

## Episode format

By default you get a single-voice episode: one host (voiced by your `soul`)
delivering an opinionated take. To get a NotebookLM-style two-host episode —
two voices in conversation, reacting to each other, with an arc — send a
`profile` on the request. `profile` is optional; omitting it is identical to
sending the single-voice default.

**The agent still writes every line.** `profile` only configures who is
speaking and how (personas, tone, engagement techniques) — it does not hand
the dialogue to an auto-writer. Grounding is absolute and per-turn: every
line of dialogue must trace back to a highlight your digest actually
surfaced, or it is dropped before you ever see it, exactly like an
ungrounded take is dropped from the single-voice script today.

```json
{
  "soul": "<markdown: your lens>",
  "context": "<plain text>",
  "episodes": [{ "video_id": "gs39QFYIbBY" }],
  "highlight_count": 4,
  "profile": {
    "name": "two-host",
    "format": "dialogue",
    "speakers": [
      { "role": "host", "name": "Host", "persona": "the soul" },
      {
        "role": "cohost",
        "name": "Cohost",
        "persona": "A sharp, skeptical foil. You defend the guest's position against the host's takes and press for specifics: whenever the host makes a claim, ask for the number, the counterexample, or the mechanism.",
        "voice_id": null
      }
    ],
    "style": {
      "tone": "sharp, argumentative, fast-paced",
      "engagement": ["interruptions", "callbacks", "disagreement", "concrete numbers"],
      "target_minutes": 5
    }
  }
}
```

- `format`: `"monologue"` (default) or `"dialogue"`. `"dialogue"` requires
  exactly one `"host"` speaker and one `"cohost"` speaker.
- A speaker's `persona` is markdown describing how THAT speaker talks — the
  host's persona defaults to the literal string `"the soul"`, meaning "use my
  `soul` itself as this speaker's voice." The cohost needs its own persona
  (there's no default foil built in beyond what you send).
- `style.engagement` is explicit config for techniques the dialogue should
  use (interruptions, callbacks, disagreement, pressing for concrete
  numbers) — borrowed structure, not an auto-writer's judgment call.
- `style.target_minutes` (1–20, default 5) caps how much dialogue gets
  written, at roughly 150 spoken words/minute.
- `voice_id` per speaker is optional; unset falls back to the service's
  `ELEVENLABS_VOICE_ID` (host) / `ELEVENLABS_COHOST_VOICE_ID` (cohost).

In the response, a dialogue script's `script.turns` is a list of
`{ "speaker": "host" | "cohost", "text", "episode_id", "segment_timestamp" }`
— resolve `episode_id` + `segment_timestamp` against `digest` exactly like a
highlight citation, because that's what grounds it. `script.monologue` still
exists for a dialogue script too: it's the readable transcript ("HOST:
...\n\nCOHOST: ...") built from `turns`, so a caller that only reads
`monologue` (as every caller could before this feature existed) still gets
something coherent. `script.takes` are the beats the dialogue was built from
(pass 1 of the two-pass outline-then-dialogue process); `script.format` tells
you which shape you got.

## Discovery

Another agent finds this service, and any persona registered on it, through
standard public documents — no credential required to read them:

- `GET /.well-known/agent.json` (and the current-spec alias
  `/.well-known/agent-card.json`) — an A2A Agent Card for the service.
- `GET /.well-known/agent-facts.json` — a NANDA AgentFacts document for the
  service.
- `GET /personas` — public personas registered on this instance.
- `GET /personas/{id}/agent.json` and `/personas/{id}/agent-facts.json` — the
  same two documents, scoped to one persona (a soul as its own agent).
- `GET /network` — the persona/show graph as data (nodes + edges), the
  DEVELOPMENT_PLAN.md §6 "infrastructure level" viewer's data source.

Registering, updating, or deleting a persona (`POST`/`DELETE /personas`)
still needs the bearer token. See `docs/DISCOVERY.md` for the full picture:
what's published where, how to register a persona, and the manual NANDA
index registration runbook.

## Minimal example

```bash
AUTH="Authorization: Bearer $CHORUS_API_TOKEN"
JOB=$(curl -s -X POST "$BASE/digest" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"soul":"# Soul...","context":"...","episodes":[{"video_id":"gs39QFYIbBY"}]}' \
  | jq -r .job_id)

# poll until terminal
curl -s -H "$AUTH" "$BASE/digest/$JOB" \
  | jq '{status, n: (.digest.episodes|length), audio: .audio_url, warnings}'
```
