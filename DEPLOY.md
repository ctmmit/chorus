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
   it can discover `run_digest` **and `chorus-tick`** (the subscription
   cron function, chorus/inngest_app.py — Phase F, docs/DEVELOPMENT_PLAN.md
   §3). Every preview deployment needs its own sync (or use the Vercel
   integration, which does this automatically per branch). `chorus-tick`
   polls every 30 minutes (`chorus.scheduler.TICK_CRON_SCHEDULE`) and fans
   out one `step.run` per due subscription (weekly and daily cadences both
   go through the same poll — see the module docstring in
   chorus/scheduler.py for why one schedule, not two).

Without Inngest configured at all, `POST /internal/cron/tick` (step 3 below,
Vercel Cron) is the fallback trigger for subscriptions — set up one or the
other, or both (harmless: a subscription only ever advances past `now` once
it runs, so a duplicate tick around the same moment just finds nothing due).

### 3. Environment variables (Vercel dashboard -> Settings -> Environment Variables, never commit)

- `CHORUS_API_TOKEN` — **required whenever a provider key is set, or
  `CHORUS_ENV=production`.** Every route except `/api/inngest` (Inngest
  signs its own requests) demands `Authorization: Bearer <token>`. The
  service refuses to start with real keys (or a declared production
  deployment) and no token. Generate one with
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- `CHORUS_ENV=production` — optional, belt-and-suspenders: refuses startup
  with no `CHORUS_API_TOKEN` even if no provider key above happens to be
  configured yet. Set this on a real deployment from day one, before you've
  necessarily added every provider key.
- `ANTHROPIC_API_KEY` — required (curation + script). Without it the service
  runs the deterministic mock and returns mock digests.
- `ELEVENLABS_API_KEY` — required for real audio. Without it `audio_url` is
  null.
- `ELEVENLABS_VOICE_ID` — optional (defaults to ElevenLabs "Rachel").
- `ASSEMBLYAI_API_KEY` — AssemblyAI speech-to-text (Universal-3.5 Pro, with
  speaker labels) on the episode's RSS enclosure audio: the primary ASR tier.
  Optional; about $0.23 per audio hour. A job is submitted then polled for up
  to 240 s, which fits one function invocation / Inngest step; a longer job
  raises a retryable provider error. Set `CHORUS_API_TOKEN` before adding this
  key: an open endpoint with a paid ASR key is an open wallet.
- `DEEPGRAM_API_KEY` — Deepgram Nova-3 STT with diarization, the backup ASR
  tier (runs when AssemblyAI is unset or fails). Optional.
- `TRANSCRIPT_API_KEY` — Supadata, native YouTube captions only
  (`mode=native`, so it can never silently bill AI transcription for an
  episode without captions). Optional; the last-resort tier for episodes with
  no RSS audio.
- **Transcript ladder.** One builder, `chorus.config_env.build_transcript_chain`,
  serves both `build_deps` (Postgres/Vercel) and `default_deps` (SQLite/local):
  fixture, then the publisher's RSS `podcast:transcript` (JSON, VTT or SRT;
  untimed `text/plain`/`text/html` are skipped), then AssemblyAI, then Deepgram,
  then Supadata, each rung present only when its key is set. The active chain
  is logged at startup. Why this order (reports/Podcast transcript sources.md,
  02 Oct 2026): transcribing the publisher's own enclosure costs about $0.29
  per 75-minute episode and covers every show with a feed, returns acoustic
  word timing and speaker labels, and rests on the acquisition route current
  case law treats most kindly (*Thomson Reuters v. ROSS*, 3d Cir., 29 Sep 2026,
  rewards using an authorized source over copying for convenience). YouTube
  captions cost about half a cent but breach YouTube's terms, fail from cloud
  IPs, carry no speakers and index a video that can differ from the podcast
  audio, so they run last. Only successes are cached, so an episode whose
  publisher transcript appears later is picked up on its next request; an ASR
  transcript already cached stays until evicted. Each ASR transcript records the
  exact audio URL it transcribed (`source_audio_url`) because dynamic ad
  insertion can shift timestamps between downloads.
- `CHORUS_ALLOW_HTTP` — set to `1` to let the RSS/Deepgram/AssemblyAI SSRF guard
  (chorus/netguard.py) accept plain `http://` URLs alongside `https://`.
  Leave unset in every real deployment; it exists only so tests and local
  dev can point at an `http://` fixture server without touching TLS. Every
  outbound URL a caller can influence (a request's `feed_url`/`audio_url`, or
  a transcript/enclosure URL a feed advertises) is still resolved and
  checked against loopback/private/link-local/reserved ranges regardless of
  this setting.
- `DATABASE_URL` — Neon's pooled Postgres DSN. Set automatically by the
  Marketplace integration (step 1); without it the service falls back to
  SQLite, which does not survive on Vercel — **do not deploy to Vercel
  without this set.** Selects the Postgres job/key/subscription/persona
  store AND transcript cache together — `chorus/config_env.py` never
  constructs a SQLite backend at all when this is set (docs/REVIEW_WAVE1.md
  #1; a stray repository-root SQLite connection attempt on Vercel's
  read-only filesystem could otherwise crash startup before serving a
  request).
- `CHORUS_STALE_JOB_SECONDS` — Postgres mode only. A shared deployment can
  have more than one live instance; the startup sweep only marks a job
  failed once it has sat untouched for this long, so a cold start never
  stomps another instance's actively-running job (docs/REVIEW_WAVE1.md #6).
  Default `1800` (30 minutes). SQLite mode always sweeps every in-flight job
  immediately — it is inherently single-process.
- `CHORUS_DB_PATH` — local-only. Overrides the repository-root default
  SQLite path; irrelevant once `DATABASE_URL` is set (no SQLite store is
  constructed in that mode).
- `BLOB_READ_WRITE_TOKEN` — Vercel Blob token. Set automatically by the
  Marketplace integration; without it artifacts fall back to local disk,
  which does not survive on Vercel — **do not deploy to Vercel without this
  set.** Uploaded with `x-vercel-blob-access: private` — audio is never a
  public URL (docs/REVIEW_WAVE1.md #5); `Job.audio_url` is always the
  relative, owner-checked `/artifacts/<name>` route, which fetches the bytes
  server-side.
- `BLOB_STORE_ID` — set automatically by Vercel on any project a Blob store
  is connected to. Lets `VercelBlobStore.get()` reconstruct a private blob's
  download URL on a cold instance that didn't itself upload the file.
  Without it, `GET /artifacts/{name}` 404s for audio this exact process
  instance never rendered (fails closed, never falls back to a public URL).
- `INNGEST_EVENT_KEY`, `INNGEST_SIGNING_KEY` — from the Inngest app (step 2).
  Without both set the service falls back to `BackgroundTasks`, which cannot
  survive a frozen function — **do not deploy to Vercel without these set.**
- `RESEND_API_KEY`, `CHORUS_EMAIL_FROM` — required for real email (self-serve
  key delivery and every subscription digest/failure email). Without both
  set, `chorus.email.get_email_sender` falls back to `MockEmailSender` and
  nothing actually sends — fine for previews, not for production. Verify the
  sending domain in the Resend dashboard before using it for real delivery.
- `CRON_SECRET` — Phase F (docs/DEVELOPMENT_PLAN.md §3): guards
  `POST /internal/cron/tick`, the subscription fan-out trigger for
  deployments without Inngest (or as a second trigger alongside it — see
  step 2). Vercel Cron (the `crons` entry in `vercel.json`) sets this
  header itself once `CRON_SECRET` is a project env var; without it set,
  the route refuses every request with `503` rather than running
  unguarded. Generate one the same way as `CHORUS_API_TOKEN`.
- `CHORUS_UNSUBSCRIBE_SECRET` — HMAC key signing unsubscribe links
  (`GET /subscriptions/{id}/unsubscribe?token=...`). Falls back to
  `CHORUS_API_TOKEN`, then a per-process random secret (logged loudly;
  existing links stop verifying after every restart) — **set this
  explicitly in production** so links in already-delivered emails keep
  working across deploys.
- `CHORUS_PUBLIC_URL` — the deployed service's own base URL (e.g.
  `https://<your-app>.vercel.app`), used to build the absolute links a
  subscription email needs (the rendered audio episode, the unsubscribe
  link) from a cron-triggered invocation that has no incoming request to
  infer its own host from. Without it set, those links fall back to
  `http://localhost:8000` outside of an HTTP request context (the Inngest
  `chorus-tick` function and its own dev/test paths) — **set this in
  production**, or every subscription email's links will be wrong.
- `CHORUS_VIEWER_URL` — base URL of the web app (`web/`), e.g.
  `https://chorus-viewer.vercel.app`. When set, the key email from `POST /keys`
  also carries a sign-in link, `<url>/subscribe#token=<key>`, in addition to
  the key text. The token sits in the URL **fragment**, never a query string:
  browsers do not send a fragment to any server, so the key stays out of access
  logs and referrers. Unset means the email carries the key text only.
- `YOUTUBE_API_KEY` — optional. A YouTube Data API v3 key (sent in the
  `x-goog-api-key` header) that lets `POST /podcasts/resolve` and the
  `resolve_podcast` MCP tool turn a YouTube `@handle`, `/c/` or `/user/` URL
  into a channel id via `channels.list` (1 quota unit per lookup). Without it
  those URL forms answer `422` and ask for the `/channel/UC...` URL; YouTube
  pages are never scraped. Channel feeds themselves need no key.
- `CHORUS_CORS_ORIGINS` — comma-separated allowlist of browser origins
  (e.g. `https://chorus-viewer.vercel.app`) allowed to call this API
  cross-origin (docs/REVIEW_WAVE1.md #13). **Required for the viewer (web/)
  deployed to a different origin than the API** — without it, no CORS
  headers are added at all and the browser blocks every cross-origin
  request, preflight included. Unset is fine for a same-origin deployment
  (the viewer proxied through the API's own origin) or non-browser callers.
- `CHORUS_MAX_JOBS_PER_DAY` / `CHORUS_MAX_INFLIGHT_JOBS` — per-owner spend
  quotas (docs/REVIEW_WAVE1.md #3), enforced before every job creation
  (`POST /digest`, `/digest/select`, `/subscriptions/{id}/run`, scheduled feed-subscription runs, MCP submit
  tools). Defaults `20` / `3`. The master token is exempt. Exceeding either
  returns `429`.
- `CHORUS_KEY_ISSUE_PER_IP_PER_HOUR` — per-client-IP token bucket on
  `POST /keys`, alongside the existing per-email-per-hour limit. Default
  `3`. **In-process only** — a serverless deployment with more than one live
  instance gets one independent budget per instance, not a shared one; the
  per-email limit (storage-backed) is the layer that IS shared.
- `CHORUS_KEY_ALLOWED_EMAIL_DOMAINS` — comma-separated allowlist of email
  domains permitted to self-serve a key (e.g. `mycompany.com`). Unset means
  any domain, matching today's open self-serve behavior — set this if
  self-serve issuance should be invite-only by domain.

`vercel.json`'s `crons` entry (`{"path": "/internal/cron/tick", "schedule":
"*/30 * * * *"}`) needs no separate setup — Vercel reads it from the repo on
deploy and starts calling it. Vercel Cron always issues a **GET** to that
path (it cannot be configured to POST), so the route accepts both; it
injects `Authorization: Bearer $CRON_SECRET` itself once that env var is
set on the project (step 3 above).

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
chorus-api               # after `pip install -e .`; loads .env.local, serves 127.0.0.1:8000
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
