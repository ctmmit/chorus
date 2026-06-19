# Build Log

> Append-only forensics. Write-mostly: the builder appends every
> iteration; humans and the supervisor read the tail. Current truth lives
> in STATE.md — if you're reading this file to figure out what's true
> now, the system has failed.
>
> Headers are machine-parseable — the supervisor greps them. Format is
> exact:
>
> `## 2026-06-12T14:30 [build] ISS-000 Title here`
>
> `[tag]` ∈ build | decision | issue | resolved | blocked | paused | review | spend
> `[paused]` is driver-written only: usage-limit backoff in progress.
> Requires no human action; resolves itself or escalates to [blocked].
> Issue entries get sequential IDs (ISS-001, ...); other tags use ISS-000.
> Blocked entries must include a `tier: A|B|C|watchdog` line in the body.

---

## 2026-06-12T00:00 [build] ISS-000 Example entry

Body. Files touched. Reasoning if non-obvious. For [decision]: the
conservative interpretation taken and the revert path. For [blocked]:
status, exact reason, proposed next action, tier. For [spend]: session
start/end usage in dollars or tokens.

---

## 2026-06-18T23:10 [build] ISS-000 Goal 1 ingest

Scaffolded the Python service (pyproject, chorus/ package). Implemented the §6
request schema (models.py: EpisodeInput, DigestRequest, Transcript), the
TranscriptProvider interface + FixtureTranscriptProvider (reads pre-transcribed
fixtures, ENGINEERING_REVIEW Q2), and ingest() with graceful skip + the
AllEpisodesFailed guard (the flagged critical gap: never empty-done).
Files: pyproject.toml, chorus/{__init__,models,transcripts,ingest}.py,
tests/test_ingest.py. 6 tests green; ruff clean. ROADMAP 1.1-1.3 done.

## 2026-06-18T23:11 [decision] ISS-000 LLM/TTS clients behind interfaces, mocked until keys

Per user directive to build past the Tier-A key gate: curation/script/audio
will call LLM/TTS through provider interfaces with deterministic mock impls,
so the full pipeline + path_test are buildable without keys. Revert path: drop
real keys in .env.local and the real provider is selected at runtime; no code
change to callers. No real paid calls made.

---

## 2026-06-18T23:30 [build] ISS-000 Goal 2 curation

LLM-client interface (llm.py): MockLLMClient (deterministic keyword-overlap
scorer driven by the soul's Attention Triggers / Curation Guidance vs Ignore)
+ AnthropicLLMClient (Haiku, activates when ANTHROPIC_API_KEY present) +
get_llm_client() factory. curation.py: window->score->threshold->Highlight with
resolving citations; below-threshold => refused ("nothing cleared the relevance
bar"); soul_version provenance hash. Models: Highlight/EpisodeDigest/Digest.
10 tests green incl. test_two_souls_diverge (the hand-validated visibility
result is now a regression: overlap <50%) and test_ungrounded_episode_refuses
(eggs + investor => refusal). ruff + mypy clean. ROADMAP 2.1-2.3 done.

---

## 2026-06-18T23:45 [build] ISS-000 Goal 3.1-3.2 script + audio

script.py: ScriptComposer iface + MockScriptComposer (one take per highlight,
take_type from the reader-recap taxonomy, traceable by construction) +
AnthropicScriptComposer (Sonnet; drops takes not grounded in a highlight).
audio.py: AudioRenderer iface + MockAudioRenderer (writes downloadable artifact;
single-voice spine floor) + PodcastCreatorRenderer stub (ElevenLabs when key).
Models: Take, Script, TAKE_TYPES. 15 tests green (traceability + refusal +
artifact). ruff + mypy clean. 3.3 (jobs + async API) next.

---

## 2026-06-19T00:05 [build] ISS-000 Goal 3.3 async job + API (Goal 3 complete)

jobs.py: SQLite JobStore (no Redis/Celery). pipeline.py: run_job orchestrates
ingest→curate→script→audio with status queued→digest_ready→done|failed; all-
transcripts-fail→failed (critical gap), audio-fail→done w/ audio_url=null
(non-fatal). app.py: FastAPI POST /digest + GET /digest/{job_id}, injectable
store+deps via create_app. Models: Job, JobStatus. 19 tests green (4 API:
job_id, full lifecycle→done, all-fail→failed, 404). ruff+mypy clean.
ROADMAP 3.1-3.3 done. Next: Goal 4 (wire path_test to API + SKILL.md + hosting).

---

## 2026-06-19T00:30 [build] ISS-000 Goal 4 end-to-end + SKILL.md (SPINE complete)

scripts/golden_path.py: executable eval driving the real API over the fixture
model (POST /digest -> poll); asserts latency <=90s, citation resolution,
missing-transcript skip (no leak), ungrounded refusal, all-fail->failed.
path_test.ps1 + path_test.sh now delegate to it -> PATH_TEST GREEN. SKILL.md:
cold-agent contract (schema, async poll, errors, source-agnostic soul recipes).
app.py: /artifacts static mount (downloadable audio). Procfile for hosting.
JobStore.close() (Windows temp cleanup). ruff+mypy+19 pytest green; path_test
green. ROADMAP 4.1-4.3 done. SPINE (Goals 1-4) COMPLETE.

---

## 2026-06-19T01:00 [build] ISS-000 Goal 5 Layer 1 soul bootstrap

bootstrap.py: source-agnostic SoulBuilder (DESIGN_DOC ladder) — MockSoulBuilder
(Tier-1 derive_from_corpus: corpus keywords -> soul schema; Tier-2
build_from_interview) + AnthropicSoulBuilder + factory. soul_origin provenance
added to DigestRequest/Digest, flowed through pipeline. Scorer fix: positive
signal now Attention Triggers + Core Interests only (Identity/Guidance prose was
polluting derived souls); negative weight raised so each soul's Ignore list
drives divergence. Two-soul divergence promoted into golden_path/path_test.
24 tests green incl. derived-finance-soul-works + offtopic-corpus-refuses.
ruff+mypy clean; path_test GREEN. ROADMAP 5.1-5.2 done.

---

## 2026-06-19T01:30 [build] ISS-000 Goal 6 Layer 2 selection

catalog.py (fixture-backed v1; resolve() seam for a real source later) +
GET /shows + POST /digest/select (resolve shows/video_ids -> DigestRequest ->
job; empty selection -> 400). 30 tests green; path_test green incl. the Goal-6
selection assertion. ROADMAP 6.1 done.

## 2026-06-19T01:35 [review] ISS-000 BUILD complete (Goals 1-6) — path_test GREEN

WHAT WAS BUILT: a thin agent-native FastAPI service (chorus/). ingest (graceful
skip + all-fail guard) -> curation (soul-conditioned scoring, resolving
citations, honest refusal, two-soul divergence) -> script (traceable opinionated
takes) -> audio (single-voice artifact) behind an async SQLite job lifecycle
(POST /digest -> poll). Layer 1: source-agnostic soul bootstrap + soul_origin
provenance. Layer 2: /shows + /digest/select. SKILL.md cold-agent contract;
Procfile + /artifacts static. LLM/TTS/transcripts are behind interfaces with
deterministic mocks (offline) and real impls (Anthropic/ElevenLabs/managed API)
that activate when keys are present.

HOW TO RUN LOCALLY:
  .venv/Scripts/python -m uvicorn chorus.app:app --reload   # serve
  bash scripts/path_test.sh                                 # golden-path eval
  .venv/Scripts/python -m pytest -q                         # 30 unit/integration

PATH_TEST: GREEN. 6 assertions over fixtures — latency <=90s, citations resolve,
missing-transcript skip (no leak), ungrounded refusal, two-soul divergence
(<50% overlap), Layer-2 selection runs to done.

WHAT REMAINS (human / out of autonomous scope):
- Provision .env.local keys to swap mocks for live Anthropic/ElevenLabs/managed
  transcript API; then verify the real audio + live-transcript paths.
- SHIP gate: 3 manual golden-path walks + cold-agent SKILL.md dogfood + record
  demo. Deploy (Railway) is a Tier-A human action.
- Goal 7 (charts/presets) deferred post-July-11 per design.

---

## 2026-06-19T02:30 [build] ISS-001 live env loader + real-LLM smoke verified

chorus/config.py: load_env() for LIVE runs (NOT imported on the package path, so
tests/path_test stay hermetic — confirmed: 30 tests still mock-green with real
keys present in .env.local). scripts/smoke_live.py: real Haiku curation + real
Sonnet script on one short episode (Gavin Baker). Result: high-quality, lens-
conditioned output (Haiku rewards falsifiable mechanisms, penalizes jargon per
Curation Guidance; Sonnet writes an opinionated investor-voiced script). The
thesis holds with real models, not just mocks. anthropic + python-dotenv added.
REMAINING (ISS-001): ElevenLabs audio renderer is still a stub — real audio is
the last unbuilt real-provider path.

---

## 2026-06-19T02:55 [resolved] ISS-001 real ElevenLabs audio

audio.py: ElevenLabsRenderer (direct ElevenLabs TTS via httpx; single-voice
spine floor; voice/model configurable). get_audio_renderer() returns it when
ELEVENLABS_API_KEY present, else MockAudioRenderer. 33 tests green (3 new:
real renderer with mocked httpx + selection). Live smoke produced a real 2.2MB
mp3 (ID3/MPEG layer III) from the Sonnet monologue. Full golden path now real
end-to-end. podcast-creator/esperanto reserved for the two-host LAYER.

---

## 2026-06-19T03:20 [build] ISS-000 deploy setup + history scrub

History: git-filter-repo purged the rotated keys from all commits (0 hits).
Deploy: chorus/app.py __main__ (python -m chorus.app loads .env.local + serves);
requirements.txt, .python-version (3.12), DEPLOY.md (Railway steps + env vars +
scope note: v1 serves the fixture catalog; live-episode transcripts via managed
API is the next integration). gates green.

---
