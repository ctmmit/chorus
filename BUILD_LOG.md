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
