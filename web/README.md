# Chorus Viewer

A Next.js (App Router, TypeScript strict) app for looking at one Chorus
digest: the episode timeline strip, highlight cards, episode player, and a
soul diff between two digests. See `docs/DEVELOPMENT_PLAN.md` §6/§8 (Phase
G) and `SKILL.md` at the repo root for the product spec and API contract.

## Running in mock mode (default, zero setup)

```bash
npm install
npm run dev
```

Open http://localhost:3000. `npm run dev` runs in **mock mode** out of the
box via the committed `.env` (`NEXT_PUBLIC_CHORUS_MOCK=1`) — no Chorus API,
no bearer token, nothing to configure. Requests are served by the
`/api/mock/*` route handlers in `app/api/mock/`, which read the fixtures in
`web/mocks/` (`job_investor.json`, `job_popculture.json`, `shows.json`,
`souls/*.md`) straight off disk. Submitting a soul containing "Pop-Culture
Critic" (the pop-culture sample) routes to the single-episode fixture;
anything else routes to the five-episode investor fixture (one of whose
requested episodes has no transcript, exercising the "skipped" path).

To regenerate those fixtures from the real pipeline (mocked LLM/TTS, real
fixture transcripts), see the repo root's fixture-capture step — it POSTs
`/digest` and `/digest/select` through `fastapi.testclient` exactly like
`tests/test_api.py` and writes the responses to `web/mocks/`.

## Running against a local Chorus API

In one terminal, from the repo root:

```bash
CHORUS_API_TOKEN=devtoken python -m chorus.app   # serves :8000
```

In another, from `web/`, create a **gitignored** `.env.local` (it overrides
the committed `.env`, so it also needs to turn mock mode off):

```bash
NEXT_PUBLIC_CHORUS_MOCK=0
NEXT_PUBLIC_CHORUS_API_URL=http://localhost:8000
```

Then `npm run dev`. Base URL and bearer token (`devtoken` above) are also
editable from the Connect panel on `/` — they're persisted to
`localStorage`, so the env var is just the initial default.

## Running against a deployed Chorus API

Same as above with `NEXT_PUBLIC_CHORUS_API_URL` pointing at the deployment
and a real `CHORUS_API_TOKEN` entered in the Connect panel.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `NEXT_PUBLIC_CHORUS_MOCK` | `1` (via committed `.env`) | `1` serves `/api/mock/*` fixtures instead of a real API. Set to `0` for any real deployment. |
| `NEXT_PUBLIC_CHORUS_API_URL` | unset | Default base URL shown on `/`'s Connect panel (only relevant when mock mode is off). The user can still override it per-browser. Also the proxy destination when `NEXT_PUBLIC_CHORUS_PROXY=1` (see below). |
| `NEXT_PUBLIC_CHORUS_PROXY` | unset (`0`) | `1` routes every API call through this app's own origin at `/api/chorus/*`, which `next.config.ts` rewrites server-side to `NEXT_PUBLIC_CHORUS_API_URL`, instead of the browser calling that URL directly. See "Deploying same-origin (no CORS)" below. |

The bearer token is never an env var — it's entered in the UI and kept only
in `localStorage`, matching SKILL.md's per-caller auth model.

## CORS: two ways to run the viewer on a different origin from the API

The viewer and the Chorus API are two separate deployments by default (the
viewer's origin, e.g. `chorus-viewer.vercel.app`, calling the API's origin,
e.g. `chorus-api.vercel.app`), which is a cross-origin request. The browser
sends a preflight `OPTIONS` request and then enforces the API's CORS
response headers before handing the real response to the page. Pick one:

### Option A — allowlist the viewer's origin on the API (`CHORUS_CORS_ORIGINS`)

Set the Chorus API's `CHORUS_CORS_ORIGINS` env var to a comma-separated list
that includes this viewer's exact deployed origin (scheme + host, no
trailing slash — e.g. `https://chorus-viewer.vercel.app`). Do this for
every environment the viewer runs in (production, each preview deployment
origin, and local dev's `http://localhost:3000` if you run the API
remotely). This is the right choice when the viewer and API are deployed
and scaled independently.

### Option B — deploy same-origin behind a Next.js rewrite (no CORS needed)

Set both:

```bash
NEXT_PUBLIC_CHORUS_PROXY=1
NEXT_PUBLIC_CHORUS_API_URL=https://your-chorus-api.example.com
```

`next.config.ts`'s `rewrites()` then proxies `/api/chorus/*` on the
viewer's own origin to `NEXT_PUBLIC_CHORUS_API_URL`, and `lib/api-client.ts`
sends every API call (including the audio artifact fetch) to that
same-origin path instead of the configured base URL directly. The browser
never makes a cross-origin request, so there's no preflight and nothing to
allowlist on the API. This is the right choice when the viewer is the only
caller of that Chorus API deployment — trade-off: the viewer's server now
sits in the request path for every API call, including the audio download.

Either way, the artifact-audio fetch only attaches the Chorus bearer token
to requests that are relative or share the configured API's exact origin
(see `lib/url.ts`'s `isSameOriginOrRelative` — docs/REVIEW_WAVE1.md #12), so
a third-party absolute `audio_url` (e.g. a Vercel Blob host) never receives
it either way.

## Deploying on Vercel

1. Import this repo into Vercel and set the project's **root directory to
   `web`** (this is a subdirectory of the `chorus` repo, not the repo root).
2. Project env vars:
   - `NEXT_PUBLIC_CHORUS_API_URL` — the deployed Chorus API's base URL.
   - `NEXT_PUBLIC_CHORUS_MOCK` — set to `0`. **Required**: the committed
     `.env` defaults this to `1` for local dev, and a platform env var only
     overrides a `.env` value if it's actually set — an unset var in Vercel
     does not "unset" the committed default.
   - `NEXT_PUBLIC_CHORUS_PROXY` — optionally set to `1` for Option B above
     (same-origin, no CORS). Leave unset for Option A (cross-origin, API
     lists this viewer's origin in `CHORUS_CORS_ORIGINS`).
3. Deploy. `/api/mock/*` and `/api/samples/souls/*` ship as ordinary
   serverless functions either way; only the first is unreachable-by-design
   once the UI stops calling it (mock mode off).

## Scripts

- `npm run dev` — dev server (mock mode by default)
- `npm run build` — production build
- `npm run lint` — ESLint (flat config, `eslint-config-next`)
- `npm run typecheck` — `tsc --noEmit` (strict)
- `npm run test` — Vitest, the pure helpers in `lib/*.test.ts`

## Structure

- `app/` — routes: `/` (connect + submit), `/jobs/[id]` (the digest),
  `/compare` (soul diff), plus `/api/mock/*` and `/api/samples/souls/*`
  route handlers used only by mock mode / the "load sample" button.
- `components/` — `SubmitForm`, `JobView`, `CompareView`, `EpisodeTimeline`
  (the timeline strip SVG), `HighlightCard`, `EpisodePlayer`, etc.
- `lib/api-types.ts` — TypeScript mirror of `chorus/models.py`; field names
  are kept identical on purpose.
- `lib/api-client.ts` — the only place that calls the API or `/api/mock/*`.
- `lib/url.ts` — pure URL helpers: resolving a returned path against the
  base URL, and `isSameOriginOrRelative` (the R12 same-origin check that
  gates attaching the Chorus bearer token).
- `lib/polling.ts` — pure helpers behind `useJob`'s bounded polling:
  the exponential-backoff-with-jitter schedule, the max-elapsed-time
  ceiling, and abort-error detection (unit-tested in `lib/polling.test.ts`).
- `lib/timeline.ts` — pure scale/format helpers for the timeline strip
  (unit-tested in `lib/timeline.test.ts`).
- `lib/storage.ts` — localStorage access (base URL, token, a navigation-only
  recent-jobs list), wrapped in try/catch.
- `lib/usage.ts` — pure helpers for Phase C's `usage` telemetry (stage
  ordering, cache-read share, skipped-episode display labels).
- `mocks/` — fixture JSON/markdown for mock mode (see above).

## Run telemetry (Phase C `usage`)

`Job.usage` (`chorus/models.py` `JobUsage`) is rendered directly — no
client-side reconstruction needed:

- **Skipped episodes** (`usage.skipped`): shown under the episode timeline
  strip with the server's own reason, for any job id, including one opened
  cold from a shared link.
- **Transcript source** (`usage.transcript_sources`): a small mono label on
  each timeline row ("fixture", "supadata", "rss:json", "deepgram", ...).
- **Stage seconds / LLM tokens** (`usage.stage_seconds`, `usage.llm_tokens`):
  a compact line under the provenance line on `/jobs/[id]`.
