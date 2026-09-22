# Development Plan — Chorus v2

Written 21 Sep 2026 against the post-review codebase (47 tests green, must-fix
items closed). Answers seven questions and orders the work by dependency, then
by ROI. Every phase has an exit criterion that is a command or an observable.

Where this plan cites external projects, the research is in §9.

---

## 0. Where we are

- A working spine: FastAPI, async job lifecycle, soul-conditioned curation with
  resolving citations, single-voice ElevenLabs episode, cold-agent SKILL.md.
- Three structural limits that every item below runs into:
  1. **Transcripts are fixture-only.** `default_deps` uses the fixture provider,
     so the deployed service can process exactly six episodes. Nothing in this
     plan is useful to an outside caller until that changes.
  2. **State is local.** SQLite for jobs and the local disk for audio. Both
     vanish on Vercel, where the filesystem is ephemeral and a function can be
     frozen the moment the response is sent.
  3. **The repo has no remote.** It lives in OneDrive with no GitHub origin.

---

## 1. Distribution: "drop a GitHub link into any agent and it builds a Chorus agent"

The target agents (Claude, ChatGPT/Codex, Grok, OpenClaw, Hermes) share two
things: they can read a repo, and most of them speak two protocols we can
publish once.

| Surface | Who consumes it | What we ship |
|---|---|---|
| **Agent Skills** (`SKILL.md`, agentskills.io) | Claude Code, Codex/ChatGPT, OpenClaw, Hermes, Cursor | Move `SKILL.md` to `skills/chorus/SKILL.md` so `npx skills add ctmmit/chorus` installs it. Add a `soul-bootstrap` skill (the interview recipe) and a `weekly-chorus` skill (how to schedule and where to send output). |
| **MCP server** | Claude, ChatGPT, OpenClaw, Hermes, Cursor, anything MCP-capable | Mount a FastMCP app at `/mcp` on the same FastAPI process. Tools: `list_shows`, `submit_digest`, `get_digest`, `build_soul`. Same auth token. |
| **OpenAPI** | Grok and any agent that can read JSON | Already free at `/openapi.json`. Add descriptions to every field; agents read them. |
| **README as a prompt** | All of them | The README's first section is literally the instruction an agent needs: "To build a Chorus agent for your principal: 1) get a token from `<url>`, 2) install the skill, 3) run the soul interview, 4) submit your first digest." Plus `AGENTS.md` (Codex convention) and `llms.txt`. |
| **Self-serve tokens** | The human behind the agent | `POST /keys {email}` mails a token (Resend). Removes the "ask Colin for a token" step that would otherwise kill cold-agent success. Rate-limit per key. |

The eval for this phase is the same one the hackathon scores: a fresh session
of each target agent, given only the repo URL, submits a digest and reads the
result with no human help. Keep a `docs/cold-agent-log.md` with one row per
agent per attempt.

## 2. Podcast-scraper best practices

The repo that matters is `chipi/podcast_scraper` (the similarly named
`augier/podcast-scraper` is an empty single-commit repo). It is a much larger
system than Chorus needs, but five of its practices map directly onto our gaps:

| Their practice | Our gap | Adopt |
|---|---|---|
| **Transcript ladder**: Podcasting 2.0 `podcast:transcript` RSS tag first, then Whisper or a cloud STT (Deepgram Nova-3, OpenAI, Gemini) | We only have fixtures | Yes. `TranscriptProvider` chain: RSS transcript → YouTube captions via managed API → Deepgram fallback. Cache every transcript in Postgres keyed by episode id. |
| **Run manifest** per job: timings, provider used, tokens, failures | Job carries no telemetry | Yes. Add `usage` to `Job`: per-stage latency, model, input/output tokens, provider. Doubles as the cost meter. |
| **Retry policy per HTTP class**, timeouts, `--max-failures` | One `raise_for_status` and hope | Yes, via Inngest step retries (Phase B) rather than hand-rolled. |
| **Grounded Insight Layer**: quotes carry evidence pointers | We already do this (citations resolve) | Keep. Their framing validates ours. |
| **MCP server for agentic access** | None | Yes (Phase C). |
| Diarization, knowledge graph, LanceDB search, Obsidian export | Not our wedge | No. |

One warning from their README: "downloaded content must remain local and not be
redistributed." Our public repo ships full caption transcripts of five
commercial shows. See §7.

## 3. Weekly email

A subscription is a stored digest request plus a schedule and a delivery
address. The pipeline already exists; this adds persistence, a trigger, and a
renderer.

- **Data:** `subscriptions {id, owner_key, email, soul, context, shows|video_ids, highlight_count, cadence, next_run_at, active}` in Postgres.
- **Trigger:** an Inngest cron (`0 13 * * 5`, Friday morning ET) fans out one
  digest job per active subscription. Each job is the existing pipeline.
- **Delivery:** on `done`, a Resend email. Content: the highlights grouped by
  episode with the "why surfaced" line and a `youtube.com/watch?v=<id>&t=<s>`
  deep link per highlight, the refused episodes listed honestly, and one
  link to the audio episode. Plain, text-first, Fulcrum-style restraint.
- **Hygiene:** one-click unsubscribe link and a `List-Unsubscribe` header
  (required by Gmail and Yahoo for bulk senders), verified sending domain.
- **Context freshness:** the stored `context` goes stale. v1: the subscriber's
  agent can `PATCH /subscriptions/{id}` with fresh context any time before the
  run. v2: a `context_url` the cron fetches.

## 4. NotebookLM-style episodes

What people mean by "like NotebookLM": two hosts, conversational, reacting to
each other, with an arc. The open-source lineage is Podcastfy (6.6k stars,
Apache 2.0) and Open Notebook's podcast feature, which is built on Podcastfy
and extracted into `podcast-creator` (the library ENGINEERING_REVIEW Q1
originally chose).

The thesis constraint still holds: **the agent writes the script.** Podcastfy
auto-writes the dialogue from source material, which produces exactly the
generic two-host recap we set out to replace. So we borrow structure, not the
writer:

| Borrow from Podcastfy / Open Notebook | How it lands in Chorus |
|---|---|
| Episode profile + speaker profiles (role, tone, voice per speaker) | `EpisodeProfile` model: host = the soul; co-host = a configurable foil ("skeptical LP", "the guest's defender"). Both voices set per profile. |
| Outline → transcript → audio, not one-shot | Composer does two passes: beats (already `takes`) then dialogue turns. Cheaper to steer and to validate. |
| Engagement techniques as explicit config (interruptions, callbacks, disagreement) | A `style` block in the profile that the composer prompt honors. |
| Longform chunking with continuity | Turns are generated per highlight cluster with a running summary; needed once episodes pass ~8 minutes. |
| Multi-speaker TTS | ElevenLabs Text-to-Dialogue (`eleven_v3`): one call with `[{text, voice_id}, ...]`, natural interplay, audio tags for tone. Replaces the single-voice call. |

Grounding rule carries over unchanged: every turn references a highlight id or
it is dropped, exactly as `AnthropicScriptComposer` does today with takes. The
`Script` model becomes `turns: list[Turn]` with `speaker`, `text`,
`highlight_ref`. Single-voice stays available as a profile.

## 5. Vercel instead of Railway

FastAPI runs on Vercel's Python runtime as one Fluid-compute function, but three
things in the current design do not survive the move:

| Today | Why it breaks on Vercel | Replacement |
|---|---|---|
| `BackgroundTasks` runs the job after the response | The function can be frozen after responding; Python has no `waitUntil` | **Inngest** (Python SDK, FastAPI integration). `POST /digest` sends an event; Inngest invokes `/api/inngest` per step with retries. Each step is its own invocation under the duration cap. |
| SQLite file | Ephemeral filesystem | **Neon Postgres** (Vercel Marketplace). Same three-column `jobs` table behind the same `JobStore` interface. |
| `artifacts/` on disk + `StaticFiles` | Ephemeral filesystem | **Vercel Blob** (or R2). `AudioRenderer.render` returns a URL; `audio_url` becomes absolute. Auth via signed URLs or keep the bearer check on a proxy route. |
| One 3-to-7-minute sequential job | Hobby caps at 300 s, Pro at 800 s | Steps: one curation step per episode (parallel), one script step, one audio step. No single step exceeds a minute or two. |

Why Inngest and not Workflow DevKit: Workflow DevKit is TypeScript-only, and
the pipeline is Python. Inngest gives durable steps, per-step retries, cron
triggers (used by §3), and a run dashboard, without rewriting orchestration.
Lighter alternative if the vendor count bothers you: Upstash QStash delivering
`POST /internal/run/{job_id}` with retries, and Vercel Cron for the weekly
trigger. Same shape, fewer features.

Fixtures stay in the bundle (read-only is fine). Preview deployments give every
branch a URL, which is what makes §7 work with one repo.

## 6. Browser visualizations

Two levels, two audiences, two different tools.

**Individual level (a principal or their agent looking at one digest).** A
Next.js + Tailwind app in `web/`, deployed on the same Vercel project, calling
the API with the caller's token. Three views, all Tufte-plain:

1. **Episode timeline strip.** One horizontal bar per episode, length = duration,
   every scored window drawn as a tick whose height is the relevance score, the
   surfaced highlights emphasized in Blue, refusals as an empty bar with the
   refusal text. Click a tick to open YouTube at that timestamp. This is the
   single most useful view: it shows the lens working.
2. **Highlight cards** beside the strip: quote, why-surfaced, score, deep link.
3. **Episode player** with the script beats listed and the current turn
   highlighted as the audio plays (turn timestamps come back from ElevenLabs).

Also a **soul diff**: two digests of the same episodes side by side, which is the
two-soul divergence test made visible. Cheap and persuasive for the demo.

**Infrastructure level (agents discovering agents).** This is the §14 North
Star surface and the NANDA context. Two pieces:

1. **Discoverability data, not pixels.** Publish an A2A agent card at
   `/.well-known/agent.json` and a NANDA AgentFacts document, signed, at a
   stable URL, and register the handle in the NANDA index. Each Chorus
   persona (soul) is itself an agent with facts: shows covered, cadence,
   episode feed URL. Without this there is nothing to visualize.
2. **The network graph.** A force-directed graph (react-force-graph, or
   d3-force if you want control) where nodes are Chorus agents and shows, and
   edges are "listens to" and, later, "reacted to" (the §14 votes and
   highlights). Sized by audience. Color by soul origin. This is the view a
   visiting agent's operator uses to find a persona worth subscribing to.
   Seed it from the registry, not from a database we own.

For operations, do not build anything: the Inngest run view and Vercel's
function logs cover it. NANDA Town's `nandatown visualize runs/<id>` evidence
HTML is the right artifact for hackathon-style interaction proofs if you enter
a Chorus agent into a Town run.

## 7. Repositories

Two repos, not because of testing, but because of what must stay private.

| Repo | Visibility | Contents |
|---|---|---|
| `chorus` | Public, MIT | Package, tests, skills, MCP server, web app, docs, two short sample fixtures you have rights to redistribute. |
| `chorus-private` | Private | Full fixture transcripts (five commercial shows' captions: personal-use territory, do not publish), your real souls and context, subscription data exports, ops runbooks. Mounted into CI as a submodule or fetched by a token. |

Testing does not need a repo. It needs branches and environments:

- `main` → production Vercel project. Protected; PRs only; CI runs pytest, ruff,
  mypy, and `golden_path.py`.
- Every PR → a Vercel preview deployment with its own URL and preview-scoped
  env vars (mock providers or a capped test key).
- `staging` branch → a second Vercel project with real keys and a separate
  token, for the cold-agent evals before they hit production.

First step regardless: `git remote add origin` and push. Today the only copy of
this repo is a OneDrive folder.

---

## 8. Phased plan

Ordered by dependency. ROI column is value divided by build effort, ranked
within the whole plan.

| Phase | Deliverable | Exit criterion | Effort | ROI rank |
|---|---|---|---|---|
| **A. Hygiene** | GitHub remote, `chorus-private` split, pinned `requirements.txt` + lockfile, keys rotated, bundle deleted, CI on PRs | CI green on a PR from a branch | 1 day | 1 |
| **B. Vercel port** | Postgres `JobStore`, Blob `AudioRenderer`, Inngest steps, preview deploys | `golden_path.py` green against a preview URL | 3 to 4 days | 3 |
| **C. Live transcripts** | Provider ladder (RSS transcript → managed captions → Deepgram), Postgres cache, `usage` telemetry on jobs | A digest over an arbitrary YouTube URL not in fixtures reaches `done` | 3 days | 2 |
| **D. Distribution** | `skills/` layout, MCP server at `/mcp`, README-as-prompt, `AGENTS.md`, self-serve tokens, cold-agent log | Claude, Codex, and Hermes each complete a digest from the repo URL alone | 3 days | 4 |
| **E. Two-host episode** | `EpisodeProfile`, dialogue `Script`, ElevenLabs Text-to-Dialogue, grounding per turn | Two-host episode renders; every turn resolves to a highlight; single-voice still passes | 3 days | 6 |
| **F. Weekly email** | Subscriptions, Inngest cron, Resend template, unsubscribe | A subscription created Monday produces a Friday email with working deep links | 2 days | 5 |
| **G. Viewer** | Next.js app: timeline strip, cards, player, soul diff | A job URL renders the strip and plays audio | 4 days | 7 |
| **H. Discovery** | A2A card, AgentFacts, NANDA registration, network graph | A second agent finds a Chorus persona through the registry and subscribes | 4 days | 8 |

Sequencing notes:

- A before everything. B before C only if you want C developed against the
  real store; otherwise C can land on SQLite and port with B.
- C before D and F. A distribution push or an email cron over six fixture
  episodes would embarrass the project.
- E and F are independent of each other and of G.
- While in B, batch curation to one Haiku call per episode with prompt caching
  on the soul block. The review estimated this cuts per-digest cost by roughly
  5x and latency from minutes to seconds; it also makes the per-episode Inngest
  step trivially short.

## 9. Sources

- podcast_scraper: https://github.com/chipi/podcast_scraper
- Podcastfy: https://github.com/souzatharsis/podcastfy
- Open Notebook podcast feature: https://www.open-notebook.ai/features/podcast
- Vercel FastAPI: https://vercel.com/docs/frameworks/backend/fastapi
- Vercel function limits: https://vercel.com/docs/functions/limitations
- Vercel fluid compute durations: https://vercel.com/changelog/higher-defaults-and-limits-for-vercel-functions-running-fluid-compute
- Vercel background-job guidance: https://www.inngest.com/blog/vercel-long-running-background-functions
- Agent Skills standard and adoption: https://inference.sh/blog/skills/agent-skills-overview
- Hermes / OpenClaw skill compatibility: https://composio.dev/content/openclaw-vs-hermes-agent
- Project NANDA: https://github.com/projnanda
- NANDA index paper: https://arxiv.org/pdf/2507.14263
- NANDA Town: https://github.com/projnanda/nandatown

---

## 10. Status — 21 Sep 2026 (execution pass)

All eight phases were built in one orchestrated pass: Phase A and the batch
curation change by the orchestrator, Phases B, C, E, F, G, H by Sonnet
subagents in isolated worktrees, Phase D by Codex. Every branch merged into
`master` with the full gate green after each merge.

| Phase | Landed | Verified by |
|---|---|---|
| A. Hygiene | pinned deps, CI workflow, private fixture split (`../chorus-private`), synthetic public fixture | pytest with and without private fixtures |
| B. Vercel port | `SqliteJobStore`/`PostgresJobStore`, `LocalArtifactStore`/`VercelBlobStore`, stage functions, `BackgroundRunner`/`InngestRunner`, `vercel.json` | 30 store/runner/Inngest tests |
| C. Live transcripts | Supadata → RSS `podcast:transcript` (JSON/VTT/SRT) → Deepgram chain, SQLite/Postgres cache, `usage` telemetry | 46 provider tests, mocked HTTP |
| D. Distribution | `skills/` layout, MCP server at `/mcp` + stdio, `POST /keys` self-serve keys via Resend, README-as-prompt, `AGENTS.md`, `llms.txt` | 11 tests; MCP mount tested against the real SDK |
| E. Two-host episodes | `EpisodeProfile`, dialogue `Script.turns`, ElevenLabs Text-to-Dialogue renderer, per-turn grounding | 19 tests incl. fake-client parser tests |
| F. Weekly email | subscriptions store/API, scheduler, Inngest tick + Vercel cron route, digest email with deep links and one-click unsubscribe | 42 tests incl. Monday→Friday scenario |
| G. Viewer | Next.js app: timeline strip, highlight cards, player, soul diff, telemetry | 47 vitest tests, lint, typecheck, build |
| H. Discovery | A2A agent card, NANDA AgentFacts, persona registry, `/network`, force-directed graph page | 19 tests; schemas checked against a2aproject/A2A v1.0.1 and projnanda/agentfacts-format |
| Batch curation | one cached Haiku call per ~40 windows instead of one per window; token telemetry | 10 parser/request-shape tests |

Totals after the review pass (22 Sep 2026): 339 Python tests, 78 web tests,
ruff and mypy clean, golden path green.

### Cross-model review (docs/REVIEW_WAVE1.md)

Codex reviewed master after the eight phases merged: 13 must-fix, 12 should-fix.
All 25 were remediated the same day on three branches (backend auth/lifecycle/
stores, transcript and content safety, viewer), each merged with the gate green.
Highlights: jobs now have owners (foreign job ids 404), per-owner quotas gate
job creation, audio is never a public blob URL (owner-checked `/artifacts`
route), no repository-root SQLite is touched in Postgres mode, the startup
sweep is conditional and age-gated, Inngest re-raises retryable failures and
marks the job failed only on final attempt, RSS/Deepgram fetches go through an
SSRF guard with byte ceilings, episode identity is one family per input with a
feed-scoped cache key, the viewer only sends the bearer token same-origin and
bounds its polling, and CORS is an explicit allowlist.

Cold-agent dogfood (docs/cold-agent-log.md): one Claude run against a local
instance completed the full loop from README + SKILL.md; its four friction
points were fixed the same day.

### Not done in this pass (needs the human or a live account)

- Push to GitHub and create the public/private repos (see the runbook in the
  final orchestrator message). The public repo's history still contains the
  five commercial transcripts in early commits; squash or filter before the
  first public push.
- Rotate the two provider keys and delete `../audience-of-one-prescrub.bundle`.
- Provision Vercel, Neon, Blob, Inngest, Resend, Supadata, Deepgram; set the
  env vars in DEPLOY.md; run the golden path against a preview URL.
- Postgres stores are only exercised when `TEST_DATABASE_URL` is set.
- Ed25519 signing of AgentFacts; NANDA index registration (manual runbook in
  docs/DISCOVERY.md).
- Per-request voice ids in `profile.speakers` are accepted but not threaded to
  the renderer (env-level voice ids are); the renderer is built once per process.
- Subscriptions are HTTP-only; no MCP tools for them yet.
