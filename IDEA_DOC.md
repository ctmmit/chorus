# IDEA_DOC.md

> The wedge document. Project for NandaHack (HCLTech · MIT Media Lab),
> in-person finale July 11, 2026 — main event only (the hosted service +
> SKILL.md, worth 80%). The Step-1 NANDA Town warm-up PR is scoped in a
> separate session.
> §6 is the v1 scope ceiling: a golden path, not demo beats. Everything
> beyond it lives in §14 as the staged North Star.

## 1. Product

**Working name: Chorus** *(alternatives: Frequency, Agora, Sidecast,
Earshot)* — "Audience of One" describes the v1 only and undersells the
vision; see §14.

A media skill for autonomous agents. An agent listens to the podcasts its
principal can't, and hands back only what matters — as a cited digest and
an opinionated audio episode voiced in the agent's own persona. **The arc:
v1 produces a podcast for an audience of one; the North Star is that the
audience grows — agent podcasts become shareable artifacts in a larger
agent conversation with its own economy (§14).**

## 2. One-sentence description + artifact type

A hosted service an AI agent calls to turn a week of podcast episodes its
principal has no time for into a persona-curated, source-cited highlights
digest and a short generated podcast where the agent gives its own takes —
not a summary, a thought partner.

**Artifact type:** `usable tool`. The main event is judged on whether a
*different* agent succeeds using only the SKILL.md, with no human help.
That is the definition of a tool built to survive a real caller, so scope
is anchored by the §6 golden path, not by demo beats. (The generated
podcast is the demo's wow moment, but it is an output of the working path,
not a staged performance — see §6b.)

## 3. Target user

The inversion that makes this a NandaHack project: **the user is an
agent, not a human.**

- **Primary caller:** an autonomous agent runtime — Colin's OpenClaw or
  Hermes agent, or any agent that can read a SKILL.md and make HTTP
  calls. It already holds the principal's connectors (Readwise, Obsidian,
  X, project context) and a persona document; what it lacks is the
  ability to ingest hours of audio and produce voiced output.
- **Principal behind the agent:** a knowledge worker who follows more
  podcasts than they can consume — e.g., an investor/operator tracking
  10+ shows weekly and currently listening to none of them in full.

The service is built for the agent. The principal is who the agent is
working for.

## 4. Painful workflow being replaced

The principal subscribes to a stack of podcasts and faces two bad options:
spend 8–10 hours a week listening, or skip them and rely on generic AI
summaries that flatten every competing take into bland consensus and lose
the one segment that actually bears on what the principal is working on.
Neither applies *their* lens. This service kills both: the agent reads
every transcript, scores each segment against the principal's persona and
live context, and returns only the segments that clear the bar — with the
exact timestamp — plus an audio episode that argues a point of view rather
than narrating bullet points. Hours of listening collapse to a five-minute
listen the principal actually wants.

## 5. Why now

Not "LLMs got better." Three things converged:

1. **Agents can now *act*, not just chat.** Runtimes like OpenClaw hold
   connectors, persistent personas, and the ability to call external
   services on a schedule. A media service has a customer that can invoke
   it unattended — which did not exist as a category two years ago.
2. **Voiced, multi-speaker generation became a public API.** Controllable
   text-to-dialogue (e.g., ElevenLabs Eleven V3) means an agent can write
   an opinionated script and have it voiced — the agent keeps authorship of
   the takes, which generic "auto-podcast" tools do not allow.
3. **The agent-service ecosystem has a discovery surface.** SKILL.md /
   NANDA-style registries mean a service can be *found and used by an agent
   on its own* — the exact thing this hackathon scores.

## 6. Golden path (the v1 scope ceiling)

One end-to-end task. v1 ships nothing off this path; v1 is not done until
it runs green on three real input sets.

- **Persona (caller):** the §3 agent, acting for one principal.
- **Inputs (checked into `/fixtures`, real):**
  - `soul.md` — the agent's persona / analytical lens (identity,
    attention triggers, communication style). One real example checked in.
  - `context.md` — a plain-text principal-context blob the *calling agent
    assembles itself* (what the principal is working on, reading, saving).
    The service does **not** integrate Readwise/X/Obsidian in v1 — it
    receives their distilled output as a parameter.
  - `episodes.json` — 5–10 podcast episodes as YouTube URLs (real shows).
- **Action:** agent `POST`s `{soul, context, episodes}` to the service,
  then polls for the job result (async; audio generation is not instant).
- **Expected output properties:**
  - *Structural:* a digest object — for each surfaced highlight,
    `{episode, segment_timestamp, relevance_score, one_line_reason}`; plus
    the generated `script` (the agent's opinionated take); plus an
    `audio_url` to the produced episode.
  - *Trust:* every highlight's reason resolves to a real transcript
    timestamp in the cited episode. A deliberately-irrelevant fixture
    episode returns an explicit "nothing cleared the relevance bar" for
    that episode — never an invented relevance reason. The audio script
    asserts only claims traceable to surfaced highlights.
  - *Latency:* digest (text) returned within a bounded time `[N]s`; the
    full audio job completes and the file is downloadable within `[M]`
    minutes. (Exact bounds set in the design pass once the TTS engine in
    §8 is chosen.)
- **Specified failure case:** an episode with no available transcript or a
  private/removed video → the service skips it, logs a structured reason,
  and completes the digest on the remaining episodes. It does not crash or
  fabricate a transcript.
- **Verification:** `scripts/path_test.sh` — green or not. Asserts the
  structural, trust, and latency properties above on all three fixture
  sets. Not lawyerable.

## 6b. Demo intent (pencil, non-binding)

The arc: hand the agent a week of shows the principal never opened. Watch
it return four highlights — and the four are unmistakably tuned to *this*
principal's current work, not a generic "top moments" list. Then hit play:
the agent doesn't recap, it takes a position, pushes back on one of the
guests, and connects two episodes the principal would never have linked.
The wow is hearing an agent that has a point of view. (Rewritten at the
SHIP gate from what the working path actually makes impressive.)

## 7. Narrowest useful wedge

Single principal (Colin), a hand-supplied list of this week's episodes, no
discovery and no live connectors: the agent returns a cited digest plus one
short voiced segment with its take. Useful to one real user tomorrow, and
it is a strict subset of §6.

## 8. Data sources

| Source | Real/mock v1 | License / cost | Ingestion difficulty |
|---|---|---|---|
| Podcast transcripts | Real | `youtube-transcript-api`, free; personal-use / ToS nuance — flag, don't productionize | Low — pipeline already exists in prior work |
| Episode metadata | Real | YouTube oEmbed, free, no key | Low |
| Curation + script | Real | Anthropic API (Claude), paid key | Low |
| **Audio / TTS engine** | Real | **OPEN — resolve in design pass** | Medium |
| Principal context | Real, **passed in** | n/a — caller's responsibility | None for the service |

**The one open decision (deliberately deferred to the design pass):** the
TTS engine. Leading candidate is **ElevenLabs Text-to-Dialogue (Eleven
V3)** — public API, multi-speaker, and critically the *agent writes the
script* so the persona's takes are preserved. Rejected-by-default:
**NotebookLM** — its official audio API is enterprise-only, the unofficial
wrappers are fragile, and it generates its *own* generic two-host script,
which defeats the "agent's own opinion" goal. This is the single biggest
data-sourcing risk (one paid dependency + the one unresolved choice); it is
flagged here, not later. *(Finance/media projects die at data sourcing — TTS
is this project's version of that.)*

## 9. AI/agentic features

Output requirements (citations resolve; ungrounded → explicit refusal) are
not policy — they are asserted properties of §6, checked by `path_test.sh`
every loop.

1. **Relevance curation** — `f(segment, soul, context) → score + reason +
   citation`. The core primitive carried over from prior work, now
   conditioned on *both* the agent's persona and the principal's live
   context. Model: Claude Haiku (per-segment, cheap, cacheable).
2. **Editorial script synthesis** — composes surfaced highlights into an
   opinionated script with a point of view, not a summary. Model: Claude
   Sonnet. Every claim traceable to a surfaced highlight.
3. **Audio generation** — voices the script. Engine per §8.

## 10. Credibility signal

For NandaHack judging (useful · creative · easy setup · agent-succeeds-
from-SKILL.md-alone) and as a portfolio artifact, this demonstrates:

- **Agent-native product design** — building *for* an agent caller, not a
  human UI; a clean SKILL.md that a cold agent can succeed from.
- **Executable evals** — `path_test.sh` asserting citation-resolution,
  ungrounded-refusal, and latency on every loop.
- **Grounding discipline** — persona-conditioned relevance with
  source-resolving citations and provenance, not vibes.
- **A genuinely fresh wedge** — an agent that produces a private podcast
  with its own opinions is a memorable, non-obvious idea (the creativity
  axis).

## 11. Non-goals (v1)

- No podcast discovery / "last 30 days" social tracking / popularity or
  interest segmentation.
- No live context connectors (Readwise, X, Obsidian, project trackers) —
  context arrives as a parameter.
- No onboarding / identity-builder flow — the SOUL arrives as a file.
- No scheduling or cadence automation — the agent decides when to call.
- No multi-user accounts, no human web UI beyond what is needed to view the
  output, no agent-to-agent registry or relevance marketplace.

## 12. Biggest risks + kill criterion

- **TTS integration (highest).** Likelihood: medium. Mitigation: treat the
  cited digest as an internal milestone that is independently green before
  audio is wired, so audio is additive, not load-bearing; fall back to
  single-voice TTS if multi-speaker dialogue slips.
- **Curation quality is subjective.** Mitigation: the golden path asserts
  *structure + citation resolution + ungrounded refusal*, never "good
  taste." Taste is demoed, not tested.
- **Transcript availability.** Mitigation: the §6 specified failure case —
  graceful skip with logged reason.
- **Scope creep into context connectors.** Mitigation: §11 makes context a
  parameter; integrating a connector is a §14 item, not a v1 patch.

**Kill criterion (resource-denominated):** if the golden path is not green
after **$[X]** of agent spend / **[N]** hours of compute, write the
postmortem as a final `[review]` entry and archive. The July 11 finale is
the external forcing function, but the kill switch is resource-denominated
per framework discipline — a path paused for life reasons isn't "late," it
just hasn't consumed its budget.

## 13. V1 scope (locked)

Every feature maps to a segment of the §6 golden path:

- `POST` ingest endpoint accepting `{soul, context, episodes}` + async
  job + poll/result endpoint. *(§6 action)*
- Transcript fetch + cache + metadata resolution; graceful skip on
  missing transcript. *(§6 inputs, failure case)*
- Curation engine — per-segment scoring with citation + provenance
  (which SOUL version produced it). *(§9.1, §6 trust)*
- Script synthesis with point of view. *(§9.2)*
- Audio generation call + downloadable artifact. *(§9.3, §6 structural)*
- Digest + script + audio_url response object. *(§6 output)*
- **SKILL.md** — the hackathon deliverable; a cold agent must succeed from
  it alone.
- **Hosting** — reachable online (Railway / Render / Fly).
- `scripts/path_test.sh` — the §6 verification.

## 14. Stretch (v2+) — the North Star

Parked, not killed. One golden path per version. **v1 serves an audience
of one; the North Star is that the audience grows.** The private podcast
stops being private and enters a larger agent conversation — the full
agent-media-economy thesis, with agent podcasts as the content artifacts.

**The far vision — a shared agent podcast economy:**

- **Shareable agent podcasts.** An episode is no longer private to its
  principal; an agent can publish it to other agents.
- **Agents as each other's audience.** Agents listen to (ingest) each
  other's podcasts, then vote, highlight, and react — quality signals that
  emerge from machine listeners, not human clicks.
- **Audience-building.** A persona with a distinctive, trusted point of
  view accumulates listeners and builds a following across the agent
  network.
- **An ad market that targets agents.** Popular podcasts attract
  advertising from companies whose customer is the *agent*, not the human
  — relevance-priced placement against an agent audience. Attention
  reconstituted on the far side of the agent boundary.
- **Infra:** the A2A content registry + relevance marketplace from prior
  work, now carrying agent podcasts plus an audience / engagement layer.

**Earlier-staged pipeline (gates before the economy):**

- **Discovery** — agent finds new shows; "last 30 days"-style social
  tracking; segmentation by interest / popularity.
- **Live context connectors** — service-side or agent-side Readwise, X,
  Obsidian, and project-tracker ingestion, replacing the v1 context blob.
- **Onboarding / identity builder** — a flow that rounds out the media
  agent's SOUL instead of taking it as a file.
- **Cadence + delivery** — scheduled weekly runs; the digest rendered and
  delivered as a newsletter.
