# Deploy (Vercel)

The service is a standard ASGI app (`chorus.app:app`), served by Vercel's
Python runtime as one Fluid-compute function. `vercel.json` sets the
function's `maxDuration`; `pyproject.toml`'s `[tool.vercel]` table points at
the entrypoint; `requirements.txt` is what Vercel installs (pinned, compiled
from `requirements.in` with `uv pip compile`).

Local dev needs none of this — see "Local dev" below.

## Why Vercel instead of a long-lived process

On a long-lived process (Railway, a VM), `POST /digest` returns immediately
and a `BackgroundTasks` job keeps running after the response. On Vercel the
function can be **frozen the instant the response is sent** and the
filesystem is **ephemeral** (gone on the next invocation). Three pieces of
the local design change to survive that, all selected purely by which env var
is present (chorus/config_env.py) — nothing to configure by hand:

| Local dev (nothing set) | Vercel (env var set) | Why |
|---|---|---|
| SQLite `chorus.db` (`SqliteJobStore`) | **Neon Postgres** via `DATABASE_URL` (`PostgresJobStore`) | The SQLite file doesn't survive between invocations. |
| `artifacts/` on disk + `/artifacts` StaticFiles | **Vercel Blob** via `BLOB_READ_WRITE_TOKEN` (`VercelBlobStore`) | Same — rendered audio must live somewhere durable. |
| FastAPI `BackgroundTasks` (`BackgroundRunner`) | **Inngest** via `INNGEST_EVENT_KEY` + `INNGEST_SIGNING_KEY` (`InngestRunner`) | A frozen function can't finish a background task; Inngest re-invokes `/api/inngest` per durable step instead. |

Each pair implements the same Protocol (`chorus.jobs.JobStore`,
`chorus.transcript_cache.TranscriptCache`, `chorus.artifacts.ArtifactStore`,
`chorus.runners.JobRunner`), so the pipeline code never branches on which
environment it's in.

## Steps

```bash
npm i -g vercel        # or: brew install vercel-cli
vercel login
vercel link             # create/link a project, from this directory
```

### 1. Provision Postgres and Blob (Vercel Marketplace)

In the Vercel dashboard → your project → **Storage**:

- **Neon Postgres**: add a Neon integration; Vercel sets `DATABASE_URL` on
  the project automatically. Use the **pooled** connection string (Neon's
  PgBouncer-fronted DSN) — `chorus/stores/postgres.py` opens a small
  connection pool of its own per function instance, and Neon's own pooling
  is what keeps that cheap across many cold starts.
- **Blob**: add a Blob store; Vercel sets `BLOB_READ_WRITE_TOKEN`
  automatically.

Both `CREATE TABLE IF NOT EXISTS` on first use — no separate migration step.

### 2. Set up Inngest

1. Create an app at [inngest.com](https://app.inngest.com) (or self-host).
2. Install the [Vercel integration](https://www.inngest.com/docs/deploy/vercel)
   *or* set `INNGEST_EVENT_KEY` and `INNGEST_SIGNING_KEY` manually (below).
3. After the first deploy, add the deployment's sync URL
   (`https://<your-app>.vercel.app/api/inngest`) in the Inngest dashboard so
   it can discover `run_digest`. Every preview deployment needs its own sync
   (or use the Vercel integration, which does this automatically per branch).

### 3. Environment variables (Vercel dashboard -> Settings -> Environment Variables, never commit)

- `CHORUS_API_TOKEN` — **required whenever a provider key is set.** Every
  route except `/api/inngest` (Inngest signs its own requests) demands
  `Authorization: Bearer <token>`. The service refuses to start with real
  keys and no token. Generate one with
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- `ANTHROPIC_API_KEY` — required (curation + script). Without it the service
  runs the deterministic mock and returns mock digests.
- `ELEVENLABS_API_KEY` — required for real audio. Without it `audio_url` is
  null.
- `ELEVENLABS_VOICE_ID` — optional (defaults to ElevenLabs "Rachel").
- `TRANSCRIPT_API_KEY` — Supadata managed captions (YouTube). Optional;
  without it the live chain skips straight to the RSS/Deepgram providers.
- `DEEPGRAM_API_KEY` — Deepgram STT, the last-resort transcript fallback
  (transcribes the episode audio directly). Optional.
- `DATABASE_URL` — Neon's pooled Postgres DSN. Set automatically by the
  Marketplace integration (step 1); without it the service falls back to
  SQLite, which does not survive on Vercel — **do not deploy to Vercel
  without this set.**
- `BLOB_READ_WRITE_TOKEN` — Vercel Blob token. Set automatically by the
  Marketplace integration; without it artifacts fall back to local disk,
  which does not survive on Vercel — **do not deploy to Vercel without this
  set.**
- `INNGEST_EVENT_KEY`, `INNGEST_SIGNING_KEY` — from the Inngest app (step 2).
  Without both set the service falls back to `BackgroundTasks`, which cannot
  survive a frozen function — **do not deploy to Vercel without these set.**

### 4. Deploy

```bash
vercel deploy            # preview deployment, own URL + preview-scoped env
vercel deploy --prod      # production
```

Every PR gets its own preview deployment with its own URL; set preview-scoped
env vars (mock providers or a capped test key) in the dashboard per
environment (Production / Preview / Development) if you want previews to
avoid spending against the real provider keys.

## Local dev

Nothing to configure: no `DATABASE_URL`, no `BLOB_READ_WRITE_TOKEN`, no
`INNGEST_EVENT_KEY`/`INNGEST_SIGNING_KEY` means SQLite (`chorus.db`) + local
`artifacts/` + in-process `BackgroundTasks` — exactly as before Phase B.
Tests and `scripts/golden_path.py` always run this way regardless of what's
in your shell environment (`chorus.config_env` only reads `os.environ`, and
nothing in the test suite sets these vars).

```bash
python -m chorus.app     # loads .env.local, serves on :8000
```

## Smoke after deploy

```bash
BASE=https://<your-app>.vercel.app
AUTH="Authorization: Bearer $CHORUS_API_TOKEN"
curl -s -H "$AUTH" "$BASE/shows" | jq '.[].show'
JOB=$(curl -s -X POST "$BASE/digest/select" -H "$AUTH" -H 'content-type: application/json' \
  -d '{"soul":"# Soul...","context":"...","shows":["20VC with Harry Stebbings"]}' | jq -r .job_id)
# Digest submission returns immediately (an Inngest event was sent); poll:
sleep 5
curl -s -H "$AUTH" "$BASE/digest/$JOB" | jq '{status, audio: .audio_url, warnings, n: (.digest.episodes|length)}'
```

`scripts/golden_path.py` itself runs fully offline (fixtures + mocks, no
network, no Vercel) — the equivalent Phase-B exit criterion is the same
script's assertions holding against a *preview URL* driven over HTTP instead
of an in-process `TestClient`; that harness is future work (§8 row B's exit
criterion references it, this repo's `golden_path.py` is the offline version
used in CI).
