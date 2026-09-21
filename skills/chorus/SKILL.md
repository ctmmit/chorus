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
    { "url": "https://www.youtube.com/watch?v=c4tvVKDhpiY" }
  ],
  "highlight_count": 4
}
```

Each episode takes `video_id` OR `url` (YouTube). Returns:

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
  "warnings": []
}
```

- Every highlight's `segment_timestamp` + `quote` resolve to the real transcript.
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
