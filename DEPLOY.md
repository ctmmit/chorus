# Deploy (Railway)

The service is a standard ASGI app (`chorus.app:app`). `Procfile`,
`requirements.txt`, and `.python-version` are set for Railway's nixpacks builder.

## Steps

```bash
npm i -g @railway/cli      # or: brew install railway
railway login
railway init               # create/link a project
railway up                 # build + deploy from this directory
```

## Environment variables (set in Railway, never commit)

In the Railway dashboard → Variables (or `railway variables set KEY=...`):

- `CHORUS_API_TOKEN` — **required whenever a provider key is set.** Every route
  (including `/artifacts`) demands `Authorization: Bearer <token>`. The service
  refuses to start with real keys and no token. Generate one with
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- `ANTHROPIC_API_KEY` — required (curation + script). Without it the service runs
  the deterministic mock and returns mock digests.
- `ELEVENLABS_API_KEY` — required for real audio. Without it `audio_url` is null.
- `ELEVENLABS_VOICE_ID` — optional (defaults to ElevenLabs "Rachel").
- `TRANSCRIPT_API_KEY` — Supadata managed captions (YouTube). Optional; without
  it the live chain skips straight to the RSS/Deepgram providers.
- `DEEPGRAM_API_KEY` — Deepgram STT, the last-resort transcript fallback
  (transcribes the episode audio directly). Optional.

Railway injects `$PORT`; the Procfile already binds it.

## Scope note (v1 -> Phase C)

`default_deps` builds a **provider chain**: fixtures first (so the demo
catalog at `GET /shows` always works offline), then whichever of Supadata
(`TRANSCRIPT_API_KEY`), an episode's own Podcasting 2.0 `podcast:transcript`
RSS tag, and Deepgram (`DEEPGRAM_API_KEY`) are configured, wrapped in a
SQLite-backed cache (`chorus.db`, table `transcripts`) so a repeat request
never re-hits a paid API. A digest over an arbitrary YouTube URL or an RSS
`{feed_url, guid}` episode reaches `done` once at least one live provider has
a key; with no keys set, only the fixture catalog resolves (as before).

## Smoke after deploy

```bash
BASE=https://<your-app>.up.railway.app
AUTH="Authorization: Bearer $CHORUS_API_TOKEN"
curl -s -H "$AUTH" "$BASE/shows" | jq '.[].show'
JOB=$(curl -s -X POST "$BASE/digest/select" -H "$AUTH" -H 'content-type: application/json' \
  -d '{"soul":"# Soul...","context":"...","shows":["20VC with Harry Stebbings"]}' | jq -r .job_id)
curl -s -H "$AUTH" "$BASE/digest/$JOB" | jq '{status, audio: .audio_url, warnings, n: (.digest.episodes|length)}'
```
