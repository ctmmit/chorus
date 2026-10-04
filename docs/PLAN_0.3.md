# Plan — Chorus 0.3: listening, learning, and the conversation between sources

Written 03 Oct 2026 against `main` at 02ce716 (0.2.0 plus writer freedom).
Ten features in three tiers, ordered by dependency and then by value over
cost. Every phase is one pull request, and every exit criterion is a command
or an observable. The four repository checks (pytest, ruff, mypy,
golden_path) pass at the end of every phase; that is assumed below and not
repeated.

## 0. What the codebase gives us, and what it lacks

The spine is complete: ingest, soul-conditioned curation with resolving
citations, a brief → outline → segment script, and audio through Chorus's
own renderers or the principal's agent. Around it sit subscriptions with
email delivery, library import, onboarding, and a persona registry with A2A
discovery documents.

Five gaps shape this plan:

1. **The episode is hard to get to.** A finished digest ends as an
   authenticated `audio_url` the agent must hand over. Nothing lands in the
   podcast app the principal already opens.
2. **The audio hides its grounding.** Every line in `Script.turns` carries
   citations with exact source timestamps, and the MP3 throws them away.
3. **The soul never learns.** It changes only when someone edits it. No
   signal flows back from what the principal found useful.
4. **Sources are scored alone.** Curation scores each episode against the
   soul independently, so the digest cannot say that three guests disagreed
   about the same thing, and it cannot remember last month.
5. **Curation quality has no number.** The golden path asserts structure
   and grounding, deliberately not taste. That was right for v1. It is now
   the thing blocking safe changes to scoring.

Two seams the plan leans on throughout: `Turn` already carries `citations`
with `episode_id` and `segment_timestamp`, and `chorus.subscriptions.Source`
is a discriminated union, so a new kind of input is one model plus one
branch in `chorus.feeds.gather_episodes`.

## 1. Order

| Phase | Feature (ideation #) | Tier | Depends on | PR branch |
|---|---|---|---|---|
| 1 | Chapters and source deep links (#4) | 1 | — | `feature/chapters` |
| 2 | Private podcast feed (#2) | 1 | 1 | `feature/podcast-feed` |
| 3 | Curation eval harness (#8) | 2 | — | `feature/curation-evals` |
| 4 | Highlight feedback and soul proposals (#1) | 1 | 3 | `feature/soul-feedback` |
| 5 | Cross-source threads (#3) | 1 | 3 | `feature/threads` |
| 6 | Running memory across weeks (#5) | 2 | 5 | `feature/claim-memory` |
| 7 | Context recipes and connectors (#6) | 2 | — | `feature/context-recipes` |
| 8 | Ask the episode (#7) | 2 | — | `feature/ask` |
| 9 | Brief me now (#10) | 3 | 1 | `feature/quick-take` |
| 10 | Personas as sources (#9) | 3 | 2, 4 | `feature/persona-sources` |

Chapters come before the feed because the feed publishes them. Evals come
before feedback and threads because both change what curation surfaces, and
without a number we cannot tell an improvement from a regression. Phases 7
and 8 have no dependencies and can run in parallel worktrees at any point.

A version bump to 0.3.0 follows phase 5. That is the point where an existing
principal notices a different product. Phases 6 to 10 ship as 0.3.x or 0.4.

---

## Phase 1 — Chapters and source deep links

**Why.** Grounding is Chorus's credibility signal, and today it is only
visible in JSON. A chapter list with a link back to each source moment makes
it visible in the player.

**Build.**

- `Turn.segment_index: int | None`, set in `_SegmentedComposer.write_script`
  and in host mode's segment assembly, so every line knows which outline
  segment it belongs to.
- `EpisodeDigest.url: str | None`, carried through from `EpisodeInput.url`
  in curation, so a chapter can link to the source.
- `chorus/chapters.py`, all pure:
  - `mp3_duration_seconds(data)`: walks MPEG frame headers (after any ID3v2
    tag) and sums frame durations. No dependency.
  - `build_chapters(script, digests, duration)`: one chapter per outline
    segment. The start is the segment's share of spoken characters before
    it, times the duration. TTS speaking rate is close to constant within an
    episode, so this lands within a few seconds, which is good enough to
    navigate by. Each chapter carries the segment name and the deep link to
    the first source moment it cites.
  - `source_link(url, seconds)`: YouTube `&t=`, Spotify `?t=` style,
    Apple Podcasts and generic RSS with the media-fragment `#t=` form. An
    unknown host returns the URL unchanged rather than a guessed parameter.
  - `write_id3_chapters(data, chapters, title)`: prepends an ID3v2.4 tag with
    `TIT2`, one `CTOC`, and a `CHAP` per chapter with an embedded `TIT2` and
    `WXXX` link. Any existing leading tag is replaced.
  - `chapters_json(chapters)`: the Podcasting 2.0 JSON chapters document,
    used by phase 2.
- `stage_audio` tags the rendered MP3 before storing it, for every renderer
  including host-plugin joins. Tagging failure is a warning, never a failed
  job.
- `Job.chapters: list[Chapter]` so API and MCP callers get the same list.

**Exit.** `pytest tests/test_chapters.py` covers the frame walker against a
synthetic MP3, chapter starts for a three-segment script, every link shape,
and a byte-level round trip of the ID3 tag. The golden path asserts that a
finished job has one chapter per outline segment and that the stored MP3
begins with an ID3v2.4 tag holding `CHAP` frames.

## Phase 2 — Private podcast feed

**Why.** Delivery should land where listening already happens. One feed URL
pasted into Overcast or Apple Podcasts once, then every digest arrives on
its own.

**Build.**

- `JobStore.list_finished(owner, limit)` on the Protocol, SQLite and
  Postgres: done jobs with audio, newest first, using the existing
  `(owner, created_at)` index.
- A per-owner feed token: HMAC of the owner under a server secret, the same
  construction as `unsubscribe_token`. Rotating the secret revokes every
  feed. The feed id is a hash of the owner, so the URL never contains an
  email address.
- Signed artifact URLs: `GET /artifacts/{name}?exp=&sig=` serves without a
  bearer token when the signature verifies. Podcast apps cannot send
  headers, so this is required for enclosures.
- `chorus/podcast_feed.py`, pure: `render_feed(jobs, base_url, ...)` returns
  RSS 2.0 with the itunes and `podcast:` namespaces. Each item gets the
  enclosure, duration, show notes listing highlights with deep links,
  `podcast:chapters` pointing at a JSON chapters route, and
  `podcast:transcript` pointing at the script as plain text.
- Routes `GET /feed/{feed_id}.xml`, `GET /feed/{feed_id}/{job_id}/chapters.json`
  and `.../transcript.txt`, all token-checked, returning 404 on any mismatch.
- MCP tool `get_podcast_feed` (registered in its own
  `register_feed_tools`) and `chorus feed` in the CLI, returning the URL and
  instructions for the main podcast apps.
- Local mode: `chorus feed` writes `~/.chorus/feed.xml` with `file://`
  enclosures, which works for desktop players. A phone needs the hosted
  route; the CLI says so instead of pretending.

**Exit.** `pytest tests/test_podcast_feed.py` validates the XML (parses,
required tags present, enclosure length matches the artifact, item order).
An integration test fetches the feed and the enclosure through the app with
no `Authorization` header and gets 200, and gets 404 with a tampered token.

## Phase 3 — Curation eval harness

**Why.** Phases 4, 5 and 6 all change what curation surfaces. This phase
makes "did it get better" a number.

**Build.**

- `fixtures/evals/`: labelled cases. Each case is a soul, a context, a
  transcript reference, and the windows a careful reader with that soul
  would surface (`must`), might surface (`ok`), and must not (`never`).
  Start with the public sample transcript and the two contrasting souls from
  Goal 5 (6 to 10 cases), then extend with private transcripts in
  `chorus-private`.
- `chorus/evals.py`, pure: precision at k, recall of `must`, the `never`
  violation count, and a refusal-correctness check for cases labelled as
  nothing-clears.
- `scripts/eval_curation.py`: runs every case through `stage_curate_episode`
  with the mock client by default, or `--live` with the configured brain,
  and writes `artifacts/evals/<rubric_version>.json`. It compares against a
  committed baseline and exits non-zero if any metric drops by more than a
  named tolerance.
- CI runs the mock eval. Live evals run by hand before merging any change to
  scoring prompts, and the PR description quotes the before and after
  numbers.

**Exit.** `python scripts/eval_curation.py` prints the table and exits 0
against the committed baseline. A deliberately broken scorer in a test
makes it exit 1.

## Phase 4 — Highlight feedback and soul proposals

**Why.** The soul should learn from what the principal kept and what they
skipped, and the principal should approve every change to it.

**Build.**

- `Highlight.highlight_id`: a stable short hash of episode id and
  timestamp, so a rating survives reruns of the same episode.
- `chorus/feedback.py`: a `FeedbackStore` Protocol with SQLite and Postgres
  implementations, one row per (owner, job, highlight) holding a vote
  (`up`, `down`) and an optional note. Re-rating overwrites.
- Inputs: MCP `rate_highlight`, `POST /feedback`, and signed one-click links
  in the digest email (`GET /feedback/{job}/{highlight}?v=up&sig=`) that
  record the vote and show a one-line confirmation page.
- `summarize_feedback(ratings, digests)`, pure: counts by show, by theme
  (the `why_surface` text), and by score band, plus the notes verbatim.
- `propose_soul_update(soul, summary)`: one model call returning a
  `SoulProposal` of concrete section edits, each with the ratings that
  justify it. It refuses below a minimum number of ratings
  (`MIN_RATINGS_FOR_PROPOSAL`) rather than overfitting to two clicks. The
  mock builds edits from the counts alone.
- `apply_soul_update(proposal_id, accept: list[int])` writes the new soul
  through the existing soul library, so `soul_version` changes and the
  origin records `feedback:<proposal_id>`. Nothing is applied without an
  explicit accept.
- The weekly scheduler adds one line to the digest email when a proposal is
  ready. The `chorus-weekly` skill teaches the agent to show the diff and
  ask.

**Exit.** Tests cover idempotent re-rating, signed-link verification, the
proposal refusal below the minimum, and that applying a proposal changes
`soul_version`. Eval: a scripted run that down-votes one theme repeatedly,
accepts the proposal, and reruns the eval shows that theme's highlights
fall without the `must` recall dropping past tolerance.

## Phase 5 — Cross-source threads

**Why.** The product is called Chorus. When several sources speak to the
same question, that conversation is the most valuable thing in the week,
and today the writer has to find it unaided.

**Build.**

- `chorus/threads.py`: after curation, one call over all surfaced
  highlights (quotes and reasons only, which is cheap) groups them into
  `Thread`s. Each thread has a question in plain words, and each member is
  a highlight id plus a stance: `agrees`, `disagrees`, `adds`. Validation
  drops any member whose id is not a real highlight and any thread with
  members from fewer than two sources. A thread is therefore always
  grounded and always cross-source.
- The mock groups by shared content words, deterministically.
- `Digest.threads: list[Thread]`.
- The outline prompt receives the threads as candidate `connection`
  segments (a kind the outline already accepts). The writer still decides
  whether to use them. A disagreement thread is offered first.
- The digest email and the web job view show threads above the
  per-source highlights.

**Exit.** Tests cover validation (an invented id is dropped, a
single-source thread is dropped) and the mock grouping. Eval: a new case
pairs two sample transcripts on the same topic and asserts at least one
thread with a `disagrees` member. The golden path asserts `threads` is
present, possibly empty.

## Phase 6 — Running memory across weeks

**Why.** An analyst remembers what was said last month. Chorus should
notice when a source repeats a take it already surfaced, and when a new
source contradicts an old one.

**Build.**

- `chorus/memory.py`: a `ClaimStore` Protocol (SQLite and Postgres) of
  surfaced claims per owner: highlight id, normalized claim text, source,
  date, thread question if any. Written at the end of each successful job.
- Retrieval without a vector database: candidate claims from the last N
  weeks are matched by content-word overlap, then the threads call (phase
  5) is given up to `MAX_REMEMBERED_CLAIMS` of them as prior context. A
  thread may then include a remembered claim as a member with stance and
  date.
- Scoring gets a named `REPEAT_PENALTY` applied to a window whose claim
  closely matches one surfaced in the last `REPEAT_WINDOW_WEEKS`. The eval
  harness tunes the constant.
- The script may refer back ("three weeks ago, X said...") only through a
  citation to the remembered highlight, which keeps grounding intact.
- `forget` controls: MCP `clear_memory` and a per-subscription opt-out.

**Exit.** Tests cover the penalty, the overlap matcher, and that a
remembered claim in a thread resolves to a stored highlight. An
integration test runs two weeks back to back and asserts the second run
references the first.

## Phase 7 — Context recipes and connectors

**Why.** `context` is the most underused input. Most principals have a
reading queue, notes and a calendar that say what they care about this
week.

**Build.** Agent-side first, since that needs no stored tokens:

- `skills/chorus-context/SKILL.md`: recipes for the host agent to assemble
  `context` from whatever it can reach: Readwise highlights from the last
  seven days, active project notes in an Obsidian vault, this week's
  calendar titles, open tasks. Each recipe states what to pull, the size
  cap, and what never to include (credentials, private messages).
- `chorus.models.ContextBlock`: an optional structured form of context
  (`source`, `items[]`, `as_of`) that the agent may send instead of a
  string. Curation renders it into the prompt and records which sources
  contributed, in `JobUsage.context_sources`.
- A `ContextProvider` Protocol for service-side pulls later, with a
  Readwise implementation behind a key. It is not wired into the hosted
  service in this phase.

**Exit.** Tests cover `ContextBlock` rendering and bounds. A cold-agent
run with the skill installed produces a job whose `context_sources` lists
at least one source.

## Phase 8 — Ask the episode

**Why.** A brief is one-way. The principal will want to ask "what did she
say about pricing?" and get an answer that is grounded the same way the
digest is.

**Build.**

- `chorus/ask.py`: `answer(question, job)` retrieves candidate windows from
  that job's transcripts (the transcript cache already holds them) by
  content-word overlap, then one model call answers using only those
  windows. Every sentence must cite a window, and quotes are cut from
  transcript text exactly as curation does. When nothing supports an
  answer, it refuses with the same honesty as curation.
- MCP `ask_digest(job_id, question)` and `POST /digest/{job_id}/ask`.
- Optional `speak=true` renders the answer through the configured voice.

**Exit.** Tests cover the refusal, the citation check (an answer citing a
window that was not retrieved is rejected), and the mock answer.

## Phase 9 — Brief me now

**Why.** A different moment from the weekly digest: a new episode drops,
and the principal wants to know in a minute whether it is worth three hours.

**Build.**

- `quick_take(episode, soul)`: curation on one episode, then a single
  script call producing a verdict (`listen`, `skim`, `skip`), three reasons
  with citations, and a 60 to 90 second monologue. No briefs, no outline.
- Chapters from phase 1 still apply, with one chapter per reason.
- MCP `quick_take` and `POST /quick-take`. The library share route
  (`/library/share`) gains `mode=quick` so a shared link from a phone
  returns a take instead of queueing for the week.

**Exit.** A latency test asserts the mock path under a named bound, and a
test asserts every reason cites a highlight.

## Phase 10 — Personas as sources

**Why.** This is the first real step toward IDEA_DOC §14: agents as each
other's audience. A persona's published digests become an input another
persona can subscribe to, and endorsements become a quality signal.

**Build.**

- Publishing: a persona may mark jobs public. A public job exposes its
  digest and audio at the persona's feed (phase 2's renderer, different
  token rules).
- `PersonaSource` in the `Source` union: subscribes to another persona's
  published highlights. `gather_episodes` turns each into an input whose
  transcript is the highlight quotes with their original citations, so
  grounding still points back to the primary source, never to the
  intermediary.
- Endorsements: when a subscriber's feedback (phase 4) up-votes a
  highlight that arrived through a persona, the persona records an
  endorsement. `/network` shows endorsement counts on edges.
- The persona's A2A card gains a `podcast` skill pointing at its feed.

**Exit.** An integration test runs persona A, publishes, subscribes
persona B to A, runs B, and asserts that B's highlights cite A's original
source episode and timestamp, and that an up-vote in B increments A's
endorsement count.

---

## Risks

- **Chapter drift.** Proportional timing can drift on episodes with long
  pauses. If it shows up in real listening, measure each render chunk's
  duration with the frame walker and snap chapter starts to chunk
  boundaries. The renderers already chunk by turn.
- **Feed token leakage.** A feed URL is a bearer credential. It never
  contains the owner, rotating the secret revokes all feeds, and phase 2
  adds `rotate_feed_token` for one owner.
- **Feedback overfitting.** The proposal minimum, explicit accept, and the
  eval check in phase 4 exist for this.
- **Thread cost.** One extra call per job over short quotes. It is logged
  in `JobUsage` and skipped when fewer than two sources surface anything.
- **Scope.** Each phase is one PR. A phase that grows past that splits, and
  the second half waits for the next slot instead of riding along.
