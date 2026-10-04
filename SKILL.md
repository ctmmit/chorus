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

- Base URL: the deployed service your operator gives you (e.g. `https://<chorus-host>`).
- Auth: send `Authorization: Bearer <token>` on **every** request, including
  `audio_url` downloads. The token is issued by whoever deployed the service.
  A missing or wrong token returns `401`. (A dev instance running only mock
  providers may have auth disabled; a deployed one with real keys never does.)
- The service holds its own provider keys server-side; you never send them.
- Every job (and its artifact) is owned by whichever token created it — the
  master token, or the email an issued key was issued to. You can only read
  your own jobs (the master token can read every job); a job id that exists
  but isn't yours 404s exactly like one that doesn't exist.
- A browser-based caller on a different origin than the API needs the
  deployment to set `CHORUS_CORS_ORIGINS` to your origin, or every
  cross-origin request (including the preflight) is blocked by the browser
  regardless of a valid token.

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
The full route table is at `GET /openapi.json` (bearer token required, like
every route except the discovery documents).

### 0. Find episodes (optional)

If you don't already have this week's episodes, the instance has a catalog:

`GET /shows` → `[{ "show": "20VC with Harry Stebbings", "episodes": [{ "video_id": "c4tvVKDhpiY", "title": "..." }] }, ...]`

You can submit straight from it with `POST /digest/select`, which takes the
same body as `POST /digest` below but with `"shows": ["<show name>", ...]`
and/or `"video_ids": ["<id>", ...]` in place of `episodes`. Episodes you submit
by bare `video_id` that are in the catalog get their `show` and `title` filled
in automatically.

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

Each episode is exactly **one identity family** — never mix a YouTube field
(`video_id`/`url`) with an RSS field (`feed_url`/`guid`/`audio_url`) in the
same episode; the service rejects that with `422` rather than guessing which
one you meant:

- `{ "video_id": "..." }` or `{ "url": "..." }` — a YouTube episode.
- `{ "feed_url": "...", "guid": "..." }` (optionally `"audio_url"` too) — a
  podcast RSS episode, identified the way Podcasting 2.0 identifies it: the
  feed plus the `<guid>` of the `<item>`. Use `{ "feed_url": "...",
  "audio_url": "..." }` instead of `guid` if that's all you have (the
  service matches on `<enclosure url>`).
- `{ "audio_url": "..." }` alone (no `feed_url`) — a direct audio file with
  no feed to match against.

An RSS episode's cache/identity key is derived from the feed URL *and* the
guid/audio_url together, so the same guid in two different feeds is always
two different episodes — never a cache collision.

The service resolves a transcript through a provider ladder: the publisher's own
RSS `podcast:transcript` file when it has timestamps, then speech-to-text on the
episode's RSS audio (AssemblyAI, with Deepgram as backup, both with speaker
labels), then existing YouTube captions as a last resort for shows with no RSS
audio. You never need to know which one fired; `usage.transcript_sources`
(below) tells you after the fact if you're curious.
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
  "chapters": [
    { "start_seconds": 0.0, "title": "Intro", "episode_id": null, "source_timestamp": null, "url": null },
    { "start_seconds": 41.2, "title": "Pricing power", "episode_id": "c4tvVKDhpiY",
      "source_timestamp": 1834.0, "url": "https://www.youtube.com/watch?v=c4tvVKDhpiY&t=1834s" }
  ],
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
  its transcript ("fixture", "rss:json"/"rss:vtt"/"rss:srt", "assemblyai",
  "deepgram", or "supadata"); `skipped` lists episodes that never resolved and why (the same
  information a skipped episode's absence from `digest.episodes` otherwise
  throws away). A `reason` prefixed `"transient: "` means the source may well
  have had a transcript — a provider call failed (timeout, auth, malformed
  response) rather than genuinely having nothing; retrying the same episode
  later can succeed. A reason with no prefix means the source itself had
  nothing (no captions, no matching feed item) and retrying won't change that.
- `chapters` lists the chapters written into the MP3 (ID3v2.4 `CHAP` frames,
  which podcast players show). A chapter starts where its outline segment
  starts, estimated from that segment's share of the spoken text, and `url`
  opens the source at the first moment the chapter cites (YouTube `t=`,
  Spotify `t=`, or `#t=` on a direct audio file; other hosts get the plain
  link). It is empty when there is no real MP3 (no TTS key, or rendering
  failed).
- An episode with nothing relevant comes back `refused: true` with
  `refusal_reason: "nothing cleared the relevance bar"` — never an invented reason.
- `audio_url` is always a RELATIVE path (`/artifacts/<name>`) under the base
  URL — never a bare public URL from whatever backs storage server-side —
  and downloading it requires the same bearer token as everything else, and
  is only permitted to the job's own owner (or the master token). It is
  unique per job. It may be `null` if audio rendering failed; `script` may
  likewise be `null` if script synthesis failed. In both cases the digest is
  still valid and `warnings` says what degraded (degrade gracefully). On a dev
  instance with no TTS key, `audio_url` points at a `.txt` placeholder holding
  the script text and `warnings` says so.

## Errors

- `401` — missing or invalid bearer token.
- `404` on `GET /digest/{job_id}` — unknown `job_id`, **or a job that belongs
  to a different principal**: every job is owned by whichever token created
  it (the master token, or the email an issued key was issued to), and a
  foreign job looks exactly like an unknown one — no way to tell "not yours"
  from "doesn't exist" by probing ids. The master token can read every job.
- `413` / `422` — body too large / a field outside the limits above.
- `429` on `POST /digest`, `POST /digest/select`, or
  `POST /subscriptions/{id}/run` — you've hit a spend quota: at most 20 new
  jobs per rolling 24h, or 3 jobs in flight at once (both per issued key; the
  master token is exempt). Wait for an in-flight job to finish, or for the
  24h window to roll forward, before retrying. `429` on `POST /keys` means
  either the per-email-per-hour issuance limit or a per-IP throttle — wait
  and retry.
- `503` on `POST /digest` / `POST /digest/select` — the job was created but
  the backend that runs it (Inngest, or the in-process runner) could not be
  reached; the response body still carries `{"job_id", "status": "failed",
  "error"}`, so the job itself is not lost — the digest cannot be produced,
  full stop, so re-submit rather than poll.
- `502` on `POST /keys` — a key was minted but the delivery email failed to
  send; the key has already been revoked server-side (it will never work),
  so retry the request rather than trying to use it.
- `status: "failed"` with `error` — e.g. none of the episodes had a retrievable
  transcript, a provider call failed before the digest existed, or the service
  restarted mid-job. Surface the error; do not retry blindly — unless every
  episode's skip reason was `"transient: "`-prefixed, in which case the
  failure is itself transient and re-submitting the same request later is
  reasonable.
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
      "engagement": ["interruptions", "callbacks", "disagreement", "concrete numbers"]
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
- `style.target_minutes` (1–20) is optional. Leave it out and the episode
  features up to three sources at about 3–4 minutes each (one source: ~6
  minutes; three: ~14). Set it and the episode features as many sources as
  fit that length with a proper introduction each, at least one. Sources
  that don't fit get a one-line mention in the close.
- `voice_id` per speaker is optional; unset falls back to the service's
  `ELEVENLABS_VOICE_ID` (host) / `ELEVENLABS_COHOST_VOICE_ID` (cohost).

Every script is written the same way, for both formats: each candidate
source gets a brief (show, title, date, people and their credentials,
context, thesis, key points), then the writer plans the episode as segments
(`script.outline`: an intro, body segments, a close, and the length it
chose in `target_minutes`). The writer decides which sources get airtime
and how much: one in depth, several in one segment, or a quick run through
many. Sources it leaves out are listed in `script.outline.also_noted`, and
`script.briefs` holds the ones it discussed. The script is written one
segment at a time, so each source is introduced the first time it comes up.

`script.turns` is the spoken script in order:
`{ "speaker": "host" | "cohost", "text", "citations", "move",
"episode_id", "segment_timestamp" }`. Each citation is
`{ "episode_id", "segment_timestamp" }`: with a timestamp it resolves
against `digest` exactly like a highlight citation; with
`segment_timestamp: null` it cites that source's brief (who, what, when).
A line with no citations is framing or a transition and states no facts.
A monologue's turns are all `"host"`. `script.monologue` is the readable
text (for a dialogue, "HOST: ...\n\nCOHOST: ..."), and `script.takes` are
the lines anchored to a specific highlight; `script.format` tells you
which shape you got.

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

Registering or deleting a persona needs the bearer token:

```json
POST /personas
{
  "name": "Fundamental investor lens",
  "description": "Weekly digest of markets and AI shows for an investor principal.",
  "soul": "<markdown: the same soul you send to /digest>",
  "shows": ["20VC with Harry Stebbings", "Sohn Conference Foundation"],
  "cadence": "weekly",
  "public": true
}
```

→ `{ "persona_id": "...", ... }`; `DELETE /personas/{id}` removes it. See
`docs/DISCOVERY.md` for what's published where and the manual NANDA index
registration runbook.

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

## Subscriptions

A subscription is a list of **sources** (podcast RSS feeds, YouTube channels)
plus a schedule and a delivery address. Create one and the service runs it
every week (or day) on its own: on each run it checks every source's feed for
episodes published since the last run, drops any it has already sent, digests
the rest, and emails the result. You do not poll and you do not re-submit.
Every write is scoped to whichever token created it: the master token sees and
edits every subscription, an issued key sees and edits only its own (another
key's subscription id 404s, exactly like an unknown one, so a key can't
enumerate other principals).

Over MCP the whole flow is three tools: `search_podcasts` (or
`resolve_podcast` for a pasted link), then `preview_subscription`, then
`subscribe`. `list_subscriptions`, `update_subscription` and `unsubscribe`
manage what exists. The HTTP routes below are the same operations.
`share_links`, `import_file` and `import_library` bring in what the principal
already follows and saves; start there when you can.

### 1. Find the sources

A source is one of four JSON shapes, discriminated on `kind`:

```json
{ "kind": "rss", "feed_url": "https://feeds.example.com/acquired.xml", "title": "Acquired", "artwork_url": null }
{ "kind": "youtube", "channel_id": "UCxxxxxxxxxxxxxxxxxxxxxx", "title": "Lex Clips" }
{ "kind": "show", "show": "20VC with Harry Stebbings" }
{ "kind": "saved", "providers": null, "title": "Saved episodes" }
```

`saved` is the principal's imported saved-episode queue; see "Start from the
principal's library" below.

`rss` is the normal case. `youtube` takes the channel id (`UC` plus 22
characters), not the @handle; episodes are listed from the channel's public
Atom feed (video ids, titles and publish times only). `show` names a catalog
show from `GET /shows`; the catalog is the static demo set, so it never gains
episodes. Feed URLs must be `https` unless the operator has set
`CHORUS_ALLOW_HTTP`.

Three routes build sources without hand-writing them:

- `GET /podcasts/search?q=acquired&limit=10` searches Apple's directory and
  returns `[{ "title", "author", "feed_url", "artwork_url", "apple_id" }]`.
  Pass `feed_url` and `title` into an `rss` source. Results are cached for an
  hour and Apple rate-limits the upstream API (about 20 calls a minute), so a
  burst of distinct queries can return `429`; retry shortly.
- `POST /podcasts/resolve` with `{ "url": "..." }` turns a link into a source.
  It accepts a direct RSS URL (fetched and checked to be RSS, title and artwork
  filled in), an Apple Podcasts show URL (`podcasts.apple.com/.../id123456`),
  and a YouTube channel URL (`youtube.com/channel/UC...`). `@handle`, `/c/` and
  `/user/` YouTube URLs resolve only when the operator has configured a YouTube
  API key; otherwise the route answers `422` and asks for the
  `/channel/UC...` URL. Anything unrecognized is a `422` with the reason.
- `POST /podcasts/import-opml` with `{ "opml": "<xml string, up to 1 MiB>" }`
  reads a podcast-app export and returns `{ "sources": [...], "skipped":
  [{ "line", "reason" }] }`. It creates nothing; review the list first.

No soul yet? `POST /souls/interview` with `{ "answers": { "identity": "...",
"interests": "...", ... } }` (the six keys under "Building a soul") returns
`{ "soul": "<markdown>" }`.

### Start from the principal's library

The principal already listens somewhere. Start from that before asking them
to search. None of these paths needs them to sign in to anything, and none
needs a particular app:

1. **Shared links.** Whenever the principal sends you an episode or show link,
   or says "save this", pass it to `POST /library/share` (MCP: `share_links`).
   Chorus becomes their save-for-later queue.
2. **Export files.** YouTube follows come from Google Takeout
   (`YouTube and YouTube Music/subscriptions/subscriptions.csv`). Podcast-app
   follows come from an OPML file: Overcast, Pocket Casts and Castro export
   one, and for Apple Podcasts an iOS Shortcut that runs "Get Podcasts from
   Library" can write one. Send either to `POST /library/import-file` (MCP:
   `import_file`).
3. **Your own connectors.** If you can already read a library the principal
   keeps elsewhere, such as a read-later app or a Spotify MCP, push its items
   to `POST /library/import` (MCP: `import_library`). Chorus never needs that
   provider's token.
4. **Nothing yet.** Fall back to search and resolve.

#### Share links

`POST /library/share` takes `{ "links": ["..."] }`, `{ "text": "..." }`, or
both, up to 100 links. `text` can be anything that contains links, such as a
share-sheet payload, a forwarded message, or the principal's own words. Each
link becomes a save dated now:

| Link | Becomes |
|---|---|
| `podcasts.apple.com/.../id<show>?i=<episode>` | an episode, matched by Apple track id |
| `podcasts.apple.com/.../id<show>` | a followed show |
| `open.spotify.com/episode/<id>`, `spotify:episode:<id>` | an episode: Spotify's public oEmbed gives the title, then Apple's episode index finds the show's feed item |
| `open.spotify.com/show/<id>` | a followed show, matched by exact title |
| `youtube.com/watch?v=`, `youtu.be/`, `/shorts/`, `/live/` | a YouTube episode, digested by video id |
| `youtube.com/channel/UC...` | a followed channel (a `youtube` source) |

The response is `{ "items": [{ "link", "title", "show_title", "item_kind",
"status", "reason" }], "skipped": [{ "link", "reason" }], "preview": { ... } }`.
Tell the principal anything in `skipped`: `spotify.link` short links and
YouTube `@handle` links can't be followed, and each comes with a reason.

A Spotify exclusive has no public feed, so it comes back `unresolved` with
that reason. Chorus never guesses a different episode. An episode title
carried by two different shows is also left unresolved; the Apple link for
the same episode resolves exactly.

**Phone share sheet.** An iOS Shortcut set to "Show in Share Sheet" takes
URLs as input and runs "Get Contents of URL": POST to
`<base>/library/share` with the header `Authorization: Bearer <key>` and the
JSON body `{"text": <Shortcut Input>}`. One tap from Apple Podcasts, Spotify
or YouTube then saves the episode. On Android, any HTTP-request share target
does the same.

#### Export files

`POST /library/import-file` takes `{ "format": "youtube_takeout" | "opml",
"content": "<file text, up to 1 MiB>" }` and returns `{ "imported",
"skipped", "preview" }`. Every followed channel or feed becomes an explicit
suggestion, ready to subscribe, and is flagged when a subscription already
covers it. Takeout columns are read by their content, so a localized header
row still works.

#### Pushing items from your own connector

`POST /library/import` takes `{ "items": [ ... ] }`, up to 500 per call:

- **Required on every item:** `provider` (`shared`, `apple`, `spotify`,
  `youtube`, `opml`, `readwise`, `instapaper` or `pushed`) and `item_kind`
  (`episode`, `show` or `document`).
- **Required unless an identifier is given:** `title`. Any of `url`,
  `feed_url`, `apple_show_id`, `spotify_id`, `youtube_video_id` or
  `youtube_channel_id` counts, and resolution fills in the title.
- **Optional:** `show_title`, `author`, `external_id`, `guid`, `audio_url`,
  `saved_at` (ISO 8601 with an offset), `consumed`, `tags`, `highlights` and
  `notes`.

`document` items, such as articles, only feed the soul. Re-importing is
idempotent, so push the whole list each time; items that already resolved
cost nothing.

As one example, a Readwise Reader library maps like this. List documents
with `category=podcast`, then for each one:

| Item field | Reader field |
|---|---|
| `provider` | `"readwise"` |
| `item_kind` | `"episode"` |
| `title` | `title` |
| `show_title` | `author` (replaced once the feed resolves) |
| `url` | `source_url` |
| `external_id` | `id` |
| `saved_at` | `saved_at` |
| `tags` | tag names |
| `consumed` | `true` when `location` is `archive` or `reading_progress` is at least 0.9 |

#### The preview

The response is a preview. Nothing is subscribed and no soul is changed:

```json
{
  "received": 72, "new": 72, "queued_episodes": 64, "pending": 0,
  "unresolved_count": 8, "unresolved": [{ "title", "show_title", "status", "reason" }],
  "suggested_sources": [{ "title": "Dwarkesh Podcast", "save_count": 9, "score": 6.1,
                          "explicit": false, "already_subscribed": false,
                          "source": { "kind": "rss", "feed_url": "...", "title": "..." } }],
  "saved_queue_source": { "kind": "saved", "providers": null, "title": "Saved episodes" },
  "corpus_items": 72,
  "next_steps": "..."
}
```

How to read the response:

- **`suggested_sources`** ranks shows by saves. Each save is weighted
  `0.5 ^ (age_days / 30)`. A show needs two saves, or a `show` item (a follow
  in another app), to qualify. Offer the principal each suggestion's `source`.
- **`saved_queue_source`** is a fourth source kind. Add it to a subscription's
  `sources` and each run also digests the newest saved episodes it hasn't sent
  before, draining a backlog a few at a time. Consumed saves and saves older
  than 120 days are skipped. Set `providers` to limit it, for example to
  `["shared"]` for links shared to Chorus.
- **`pending`** means Apple's rate limit (about 20 lookups a minute) stopped
  resolution partway through. Import again in a minute to finish.
- **`unresolved`** lists each item that could not be matched, with the reason.
  Usually the show has no public feed (a Spotify exclusive, a paywalled show).

`GET /library/items?status=resolved|pending|unresolved|corpus&limit=50` lists
what is stored (MCP: `list_library_items`). `POST /library/soul` (MCP:
`soul_from_library`) proposes a soul from the titles, tags, notes and
highlights. It returns `{ "soul", "based_on" }` and saves nothing, so show it
to the principal before using it.

### 2. Preview

`POST /subscriptions/preview`

```json
{ "sources": [ ...source objects... ], "lookback_days": 7, "max_episodes_per_run": 8 }
```

returns `{ "episodes": [{ "source_title", "title", "published_at", "episode" }],
"errors": [{ "source", "reason" }] }`: exactly the episodes the first run would
digest, newest first, capped round-robin across sources, plus any source that
could not be read. Show the principal the list and drop sources that errored.
Nothing is saved.

### 3. Create

`POST /subscriptions`

```json
{
  "email": "you@example.com",
  "soul": "<markdown: your lens>",
  "context": "<plain text>",
  "sources": [ ...source objects... ],
  "cadence": "weekly",
  "highlight_count": 4,
  "max_episodes_per_run": 8,
  "lookback_days_first_run": 7,
  "notify_when_empty": true
}
```

Give exactly one of `sources`, `episodes` or `shows`; `sources` takes 1 to 50
entries. `cadence` is `"weekly"` (next Friday 13:00 UTC) or `"daily"` (next
13:00 UTC); `profile` works exactly as in `POST /digest`. The response is the
stored subscription: `subscription_id`, `next_run_at`, and the run state
described next.

### What each run does

1. Lists every source and keeps episodes published after the previous run
   (`last_run_at`). The first run looks back `lookback_days_first_run` days
   (1 to 30, default 7).
2. Drops any episode whose id is in `seen_episode_ids`, the ids already sent
   (the most recent 2,000 are kept), so an episode is never digested twice.
3. Takes at most `max_episodes_per_run` (1 to 20, default 8) round-robin across
   sources, so one prolific show cannot crowd out the others. Episodes the cap
   leaves out are not carried over; the email says how many were left out.
4. Creates one digest job for those episodes, counted against the owner's job
   quota like any other, and emails the result with highlights grouped by show.
5. Records `last_run_summary`: `{ "ran_at", "new_episodes", "job_id",
   "skipped_reason" }`.

A run with nothing new creates no job. With `notify_when_empty: true` (the
default) it emails a short "Nothing new from your shows this week" note listing
the sources it checked; with `false` it stays silent. Either way
`last_run_summary.skipped_reason` is `"no new episodes"` and the schedule
advances. A source that cannot be read never stops the run: it is named, with
the reason, in the email's "Could not check" footer, and when every source
fails the cursor does not advance so the next run covers the gap. A digest job
that fails emails a short failure notice and leaves the cursor and seen list
alone, so the same episodes are retried on the next run.

The older `episodes` (an explicit list) and `shows` (catalog names) forms are
still accepted. They re-digest the same fixed set on every run and never
discover new episodes; use `sources` for a recurring digest.

### Change it

`PATCH /subscriptions/{id}` any time before the next run; only the fields you
send change:

```bash
curl -s -X PATCH "$BASE/subscriptions/$SUB_ID" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"context":"<this week'"'"'s projects, reading, priorities>"}'
```

It accepts `context`, `active` (pause or resume without deleting), `cadence`,
`highlight_count`, `sources`, `max_episodes_per_run` and `notify_when_empty`.
Replacing `sources` keeps `seen_episode_ids`, so nothing already sent repeats.

### List, inspect, run now, delete

- `GET /subscriptions` returns every subscription you (or, with the master
  token, anyone) own.
- `GET /subscriptions/{id}` returns one.
- `POST /subscriptions/{id}/run` runs it immediately (bypassing the schedule)
  and returns `{ "job_id", "skipped_reason" }`; `job_id` is `null` when there
  was nothing new. Poll `GET /digest/{job_id}` as usual. It also advances
  `next_run_at` and sends the same email a scheduled run would.
- `DELETE /subscriptions/{id}` stops and removes it.

### Delivery and unsubscribe

The email carries highlights grouped by show with a working per-highlight link
(`youtube.com/watch?v=<id>&t=<s>s` for YouTube episodes, the plain `audio_url`
for RSS episodes), refused and skipped episodes listed honestly, one link to
the rendered audio episode, and the lens's provenance (`soul_version`,
`soul_origin`).

Every email carries a one-click unsubscribe link
(`GET /subscriptions/{id}/unsubscribe?token=<sig>`, HMAC-signed, no API
token required, since it is meant to be clicked from an email client) and the
`List-Unsubscribe` / `List-Unsubscribe-Post` headers Gmail and Yahoo require
of bulk senders. Clicking it deactivates the subscription (`active: false`);
clicking it again is a no-op, not an error.

## Podcast feed

Every finished digest with rendered audio also lands in the principal's
private podcast feed, so it arrives in the podcast app they already use.

- `GET /feed` (bearer token, like every other route) returns
  `{feed_url, episodes, instructions}`. The MCP tool `get_podcast_feed` returns
  the same. Give the principal `feed_url` once; they add it to a podcast app
  that accepts private feeds (Overcast, Pocket Casts, Apple Podcasts on macOS).
- The feed URL is `/feed/<token>.xml`. The token is an HMAC of the owner, so
  the URL contains no email address, needs no `Authorization` header (podcast
  apps cannot send one), and is a credential: anyone holding it can hear the
  digests. Rotating `CHORUS_FEED_SECRET` revokes every feed.
- Each item carries the MP3 (byte-range requests supported), its duration,
  show notes listing the highlights with links that open each source at the
  cited moment, Podcasting 2.0 `podcast:chapters`, and `podcast:transcript`
  (the script as text). Directories are told not to list it (`itunes:block`,
  `podcast:locked`).
- Local installs have no public URL: `chorus feed` (or `get_podcast_feed` on
  the local MCP server) writes `~/.chorus/feed.xml` with `file://` enclosures
  for a desktop player.
