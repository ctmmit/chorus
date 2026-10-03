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

## Subscribing without an agent (`/subscribe`, `/subscriptions`)

A person with no agent can set up a recurring digest in four screens and
never touch JSON or curl. The header links to both pages.

**`/subscribe`** is a four-step wizard. The step indicator is a row of
buttons (Tab, Enter) and the current step is kept in the URL fragment
(`#step=N`); everything entered is saved to `localStorage` under
`chorus.subscribeDraft`, so a reload or a return visit resumes where the
person left off. A step can only be opened once every earlier step is
complete.

1. **You.** Sign in with a key. Enter an email and the API emails a key
   (`POST /keys`); paste the key back, or click the link in the email.
   "Already have a key?" is always available. The pasted key is verified with
   one authenticated read before it is kept.
2. **Shows.** `SourcePicker`: search by name (debounced 300 ms, 2+ characters),
   paste a link (RSS, Apple Podcasts show, or YouTube channel, resolved by
   `POST /podcasts/resolve`, with a 422 `detail` shown inline), or import an
   OPML export from a podcast app (`POST /podcasts/import-opml`, with imported
   and skipped counts). The chosen list is de-duplicated by feed URL, channel
   id, or show name. Next stays disabled until there is at least one show.
3. **Your lens.** `SoulBuilder`: answer the six interview questions
   (`POST /souls/interview` writes the soul markdown) or paste a soul, then
   edit it in place. A "what are you working on this week?" box sets the
   context.
4. **Schedule & preview.** Weekly (Friday) or daily, highlights per episode
   (1 to 20), episodes per digest (1 to 20), single voice or two hosts (the
   `TWO_HOST_PROFILE` from `chorus/models.py`), delivery email, and "email me
   even when nothing is new". Preview (`POST /subscriptions/preview`) lists the
   episodes the first digest would cover, grouped by show, plus any feeds that
   could not be read. Subscribe (`POST /subscriptions`) shows the next run in
   `DD MMM YYYY HH:mm` local time, a "Run first digest now" button
   (`POST /subscriptions/{id}/run`, then `/jobs/{job_id}`), and a link to
   `/subscriptions`.

**`/subscriptions`** lists the signed-in person's subscriptions: the shows,
cadence, next run, active state, and the last run (new-episode count and a
link to its job, or the reason it was skipped). Each row can pause or resume,
edit its context inline, edit its shows (the same `SourcePicker`), run now, and
delete (with an inline confirm).

### The sign-in link

When the operator sets `CHORUS_VIEWER_URL` on the Chorus **API** (for example
`https://chorus-viewer.vercel.app`), the key email also carries a link to
`<CHORUS_VIEWER_URL>/subscribe#token=<key>`. Opening it stores the key,
removes it from the address bar with `history.replaceState` (so it never
lands in history or a screenshot), and skips ahead to the first incomplete
step. The key lives in `localStorage` like the Connect panel's token and is
sent only to the configured API (or the same-origin proxy). Without
`CHORUS_VIEWER_URL` the email has only the key, which the person pastes in
step 1.

The API address comes from `NEXT_PUBLIC_CHORUS_API_URL` (or
`NEXT_PUBLIC_CHORUS_PROXY=1`). If neither is set, step 1 shows an
"Advanced: Chorus API address" field.

### Mock mode behavior

With `NEXT_PUBLIC_CHORUS_MOCK=1` (the committed default) the whole flow is
clickable with no backend. The handlers in `app/api/mock/` are thin wrappers
over pure, unit-tested logic in `lib/server/mock-subscribe.ts`:

- `POST /keys` returns 202 and sends nothing. Step 1 says so and offers a
  "Use the demo key" button; any text works as a key.
- `GET /podcasts/search` matches `mocks/podcast_catalog.json` (twelve business
  podcasts with plausible feed URLs; cover art is a generated placeholder from
  `/api/mock/artwork`, never a hot-linked image).
- `POST /podcasts/resolve` recognizes the three link forms: a feed-shaped URL,
  `podcasts.apple.com/.../id<digits>` (looked up in the catalog by Apple id),
  and `youtube.com/channel/UC...` or `youtube.com/@handle`. Anything else is a
  422 with an explanatory `detail`.
- `POST /podcasts/import-opml` parses the real OPML text posted to it.
  `mocks/subscriptions_sample.opml` is a sample export with folders, an
  `&amp;` title, a duplicate feed, and two entries it must skip; "Use the
  sample file instead" (mock mode only) imports it without a file chooser.
- `POST /subscriptions/preview` returns a deterministic week of episodes per
  show (RSS episodes carry `feed_url`, `guid`, `audio_url`). A source whose
  address contains `unreachable`, or a feed that starts with `http://` (the
  API fetches https only), comes back under `errors` for that source instead.
  The wizard lists those errors under the preview; the picker and
  `/subscriptions` flag `http://` feeds inline.
- `POST /subscriptions/{id}/run` answers `{job_id, skipped_reason}` like the
  real API. A second run within a minute, or a subscription whose sources are
  all unreadable, returns `job_id: null` and a reason such as "no new
  episodes; 1 of 2 source(s) could not be read"; the UI shows that reason
  instead of navigating to `/jobs`.
- `POST /souls/interview` renders the same six-section template as
  `chorus/bootstrap.py`.
- `/subscriptions` handlers keep subscriptions in memory on the dev server
  (two are seeded) until it restarts. "Run now" points at a fixture job so
  `/jobs/{job_id}` renders a real digest.

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
- `npm run test` — Vitest, the pure helpers in `lib/**/*.test.ts`

## Structure

- `app/` — routes: `/` (connect + submit), `/jobs/[id]` (the digest),
  `/compare` (soul diff), `/subscribe` (the human wizard), `/subscriptions`
  (manage), plus `/api/mock/*`, `/api/samples/souls/*` and
  `/api/samples/opml` route handlers used only by mock mode / the sample
  buttons.
- `components/` — `SubmitForm`, `JobView`, `CompareView`, `EpisodeTimeline`
  (the timeline strip SVG), `HighlightCard`, `EpisodePlayer`, etc. The
  subscribe flow: `SubscribeWizard` (steps `SignInStep`, `SourcePicker`,
  `SoulBuilder`, `ScheduleStep`, with `StepIndicator`), `SubscriptionsView` /
  `SubscriptionRow`, and shared `ArtworkThumb`, `NumberField`, `ui.ts` (class
  strings).
- `lib/subscribe.ts` — pure helpers for the flow: wizard-step validation and
  reachability, source de-duplication, fragment-token parsing, `DD MMM YYYY`
  date formatting, request building, preview grouping, draft
  (de)serialization (`lib/subscribe.test.ts`). `lib/api-errors.ts` turns a
  failed call into a sentence and flags rejected keys.
- `lib/server/mock-subscribe.ts` + `mock-store.ts` — mock-mode logic and the
  in-memory subscription store (`lib/server/mock-subscribe.test.ts`).
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
- `mocks/` — fixture JSON/markdown for mock mode (see above), including
  `podcast_catalog.json` and `subscriptions_sample.opml` for the subscribe flow.

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
