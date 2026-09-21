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
| `NEXT_PUBLIC_CHORUS_API_URL` | unset | Default base URL shown on `/`'s Connect panel (only relevant when mock mode is off). The user can still override it per-browser. |

The bearer token is never an env var — it's entered in the UI and kept only
in `localStorage`, matching SKILL.md's per-caller auth model.

## Deploying on Vercel

1. Import this repo into Vercel and set the project's **root directory to
   `web`** (this is a subdirectory of the `chorus` repo, not the repo root).
2. Project env vars:
   - `NEXT_PUBLIC_CHORUS_API_URL` — the deployed Chorus API's base URL.
   - `NEXT_PUBLIC_CHORUS_MOCK` — set to `0`. **Required**: the committed
     `.env` defaults this to `1` for local dev, and a platform env var only
     overrides a `.env` value if it's actually set — an unset var in Vercel
     does not "unset" the committed default.
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
