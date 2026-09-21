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
- `TRANSCRIPT_API_KEY` — for the live transcript path (see scope note below).

Railway injects `$PORT`; the Procfile already binds it.

## Scope note (v1)

`default_deps` currently uses the **fixture** transcript provider, so a deployed
v1 resolves transcripts only for the episodes in `fixtures/transcripts/` (the
demo catalog exposed at `GET /shows`). Fetching transcripts for arbitrary live
episodes needs the managed-API transcript provider wired behind
`TranscriptProvider` (the next real-integration piece, analogous to how audio
was). Until then, drive the demo via `POST /digest/select` over `GET /shows`, or
`POST /digest` with the fixture video ids.

## Smoke after deploy

```bash
BASE=https://<your-app>.up.railway.app
AUTH="Authorization: Bearer $CHORUS_API_TOKEN"
curl -s -H "$AUTH" "$BASE/shows" | jq '.[].show'
JOB=$(curl -s -X POST "$BASE/digest/select" -H "$AUTH" -H 'content-type: application/json' \
  -d '{"soul":"# Soul...","context":"...","shows":["20VC with Harry Stebbings"]}' | jq -r .job_id)
curl -s -H "$AUTH" "$BASE/digest/$JOB" | jq '{status, audio: .audio_url, warnings, n: (.digest.episodes|length)}'
```
