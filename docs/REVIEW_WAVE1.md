# Chorus Wave 1 Goal-Boundary Review

Cross-model review by Codex (gpt-5 family) on 21 Sep 2026 of master after Phases A-H merged. Remediation status is tracked in §10 of docs/DEVELOPMENT_PLAN.md.


## Must-fix

### 1. The Vercel/Postgres configuration still opens repository-root SQLite databases

- **File:line:** `chorus/pipeline.py:75-106`, `chorus/config_env.py:77-93`, `chorus/app.py:84-93`, `chorus/jobs.py:18`, `chorus/keys.py:39-49`
- **Severity:** must-fix
- **Concrete failure scenario:** Deploy with `DATABASE_URL` set. `build_deps()` first calls `default_deps()`, which constructs `SqliteTranscriptCache()` at the repository-root default path before replacing it with `PostgresTranscriptCache`. `create_app()` separately creates `SqliteKeyStore(DEFAULT_DB)` because `PostgresJobStore` has no `db_path`. Both SQLite constructors immediately connect and run DDL. Vercel Functions expose a read-only filesystem except for `/tmp`, so module import or application startup can fail before the API serves a request. See [Vercel runtime filesystem documentation](https://vercel.com/docs/functions/runtimes).
- **Suggested fix:** Select each dependency before constructing it. When `DATABASE_URL` is present, never instantiate the SQLite transcript cache or key store. Add a Postgres key store, or place a deliberately ephemeral SQLite store under `/tmp` only if losing issued keys is acceptable. Add a deployment-mode startup test that fails if any repository-root SQLite connection is attempted.

### 2. Paid transcript providers can be enabled while the entire API remains unauthenticated

- **File:line:** `chorus/app.py:49-61`, `chorus/app.py:95-103`, `chorus/pipeline.py:62-63`, `chorus/pipeline.py:90-101`
- **Severity:** must-fix
- **Concrete failure scenario:** Configure `TRANSCRIPT_API_KEY` or `DEEPGRAM_API_KEY`, but omit `CHORUS_API_TOKEN`, `ANTHROPIC_API_KEY`, and `ELEVENLABS_API_KEY`. The lifespan guard only recognizes the latter two provider variables, so startup succeeds. Because the middleware enforces bearer authentication only when `CHORUS_API_TOKEN` is non-null, an unauthenticated caller can submit `/digest` jobs or call the mounted MCP tools and spend Supadata or Deepgram credits.
- **Suggested fix:** Treat every credential that can cause provider spend as a protected-provider key, including transcript, email, and blob credentials as appropriate. Prefer an explicit production mode in which missing API authentication always prevents startup, regardless of which providers happen to be configured. Add startup matrix tests for every provider-key combination.

### 3. Public key issuance provides no meaningful control over provider spend

- **File:line:** `chorus/app.py:123-147`, `chorus/keys.py:51-77`, `chorus/email.py:42-57`
- **Severity:** must-fix
- **Concrete failure scenario:** An unauthenticated caller submits a disposable email to `POST /keys`, receives a valid bearer key, and then submits an unlimited number of maximum-size digest jobs. The one-hour limit applies only to issuing another key for the same normalized email; it does not limit requests, concurrent jobs, episodes, or provider cost per issued key. Rotating email addresses also permits repeated Resend calls, so the issuance endpoint itself can consume paid email capacity.
- **Suggested fix:** Put issuance behind an allowlist, verified invitation, CAPTCHA plus abuse controls, or another explicit trust gate. Persist per-principal quotas and enforce request, concurrency, episode, and provider-budget limits before job creation. Add IP/device throttling to issuance and operational spend ceilings independent of application-level limits.

### 4. Jobs have no owner, so every valid key can read every known job ID

- **File:line:** `chorus/app.py:150-161`, `chorus/jobs.py:25-31`, `chorus/jobs.py:55-58`, `chorus/models.py:480-513`, `chorus/mcp_server.py:79-84`
- **Severity:** must-fix
- **Concrete failure scenario:** Caller A submits a private episode and receives a job UUID. If that UUID appears in a shared URL, log, browser history, support message, or analytics event, caller B can use any valid issued key to fetch A's complete job through `GET /digest/{job_id}` or the MCP `get_digest` tool. Neither the job record nor either read path records or checks the authenticated principal.
- **Suggested fix:** Derive a stable principal from the master token or issued-key hash, store `owner_id` on every job, and require an owner match on all HTTP, MCP, artifact, and retry operations. Reserve an explicit administrator role for cross-owner access. Migrate existing rows conservatively and add two-principal isolation tests.

### 5. Vercel Blob audio is public and bypasses the API's bearer check

- **File:line:** `chorus/artifacts.py:24-32`, `chorus/artifacts.py:90-106`, `chorus/app.py:190-193`, `chorus/models.py:498-501`
- **Severity:** must-fix
- **Concrete failure scenario:** A production render is uploaded with `x-vercel-blob-access: public`, and the public URL is returned in `Job.audio_url`. Anyone who obtains that URL can download the private audio without a Chorus bearer token, even though the model describes artifact URLs as authenticated and the local artifact route is behind application middleware.
- **Suggested fix:** Store private artifacts and serve them through an authenticated, owner-checked download route or short-lived signed URLs. If the storage product cannot provide private objects, use opaque, expiring delivery URLs and document the residual exposure instead of presenting them as authenticated resources.

### 6. Every application startup can mark another worker's active Postgres jobs failed

- **File:line:** `chorus/app.py:95-111`, `chorus/jobs.py:20-22`, `chorus/stores/postgres.py:75-90`
- **Severity:** must-fix
- **Concrete failure scenario:** In a shared Postgres deployment, instance A is actively processing a `queued` or `digest_ready` job through Inngest. A cold start of instance B runs `fail_in_flight()` and marks every such row failed. The sweep first reads complete job payloads and later saves them, so it can also overwrite a newer `done` update from instance A with stale failed state.
- **Suggested fix:** Remove the global startup sweep. Track leases with worker ID and heartbeat timestamps, and recover only rows whose lease has expired using a conditional update such as `WHERE status IN (...) AND lease_expires_at < now()`. Make terminal transitions compare-and-set operations so stale writers cannot replace terminal state.

### 7. The Inngest path acknowledges unexpected and transient failures instead of retrying them

- **File:line:** `chorus/inngest_app.py:200-218`, `chorus/transcripts.py:407-430`, `chorus/ingest.py:22-41`
- **Severity:** must-fix
- **Concrete failure scenario:** A transient LLM, database, or audio exception escapes `_execute()`. `run_digest_body()` catches it, writes `failed`, and returns normally, so Inngest records a successful function invocation and performs no retry. Transcript outages are even more aggressively downgraded: the provider chain catches both `TranscriptNotFound` and `TranscriptProviderError`, eventually raises `TranscriptNotFound`, and ingestion converts an all-provider outage into terminal `AllEpisodesFailed`.
- **Suggested fix:** Define retryable and terminal exception classes. Persist diagnostic state, then re-raise retryable failures so Inngest retries them; only acknowledge deterministic terminal failures. Preserve `ProviderError` when all providers failed transiently instead of converting it to `NotFound`. Make finalization idempotent and use conditional state transitions so a retry cannot regress a completed job.

### 8. A runner submission error leaves a real job permanently queued

- **File:line:** `chorus/app.py:150-154`, `chorus/app.py:167-181`, `chorus/runners.py:76-85`
- **Severity:** must-fix
- **Concrete failure scenario:** The API saves a new `queued` job and then `InngestJobRunner.submit()` fails because Inngest is unavailable. The route returns 500 without the job ID, but the row remains queued and has no scheduled execution. It reaches a terminal state only if a later process startup happens to run the unsafe global sweep.
- **Suggested fix:** Make job creation and dispatch an outbox transaction, then have a reliable dispatcher retry unsent events. As a minimum, catch submit failures, conditionally transition that job to `failed`, and return its ID and error state. Test failures before send, ambiguous send success, and repeated delivery.

### 9. MCP submissions bypass the configured job runner and execute the whole pipeline inline

- **File:line:** `chorus/mcp_server.py:35-49`, `chorus/mcp_server.py:90-93`, `chorus/mcp_server.py:115-130`
- **Severity:** must-fix
- **Concrete failure scenario:** In a hosted deployment configured to use Inngest, an authenticated MCP caller invokes `create_digest`. The MCP server creates the job but directly awaits `run_job()` rather than submitting through the selected `JobRunner`. A long transcript, LLM, or audio operation can exceed the request lifetime; termination then leaves work incomplete and defeats Inngest retries and step durability.
- **Suggested fix:** Inject the same selected `JobRunner` into the MCP server and make MCP submission semantics match the HTTP API. Return the job immediately and require polling. Keep an explicit inline runner only for local development and tests.

### 10. RSS ingestion permits server-side requests to arbitrary network targets

- **File:line:** `chorus/models.py:86-99`, `chorus/transcripts.py:171-184`, `chorus/transcripts.py:237-284`, `chorus/ingest.py:27-33`
- **Severity:** must-fix
- **Concrete failure scenario:** A caller supplies `feed_url=http://127.0.0.1:...`, a cloud metadata address, or a public feed that advertises a private-network `podcast:transcript` URL. The server performs both requests with `httpx` and parses the responses. Status and parsing differences provide a blind internal-network probe; a suitably shaped internal response can also enter the transcript and downstream LLM pipeline.
- **Suggested fix:** Permit only `https` URLs, resolve DNS before every connection, reject loopback/private/link-local/multicast/reserved addresses for IPv4 and IPv6, and revalidate after redirects. Apply an outbound proxy or network egress policy as a second boundary. Treat feed-provided transcript URLs with the same policy as caller-provided URLs.

### 11. Ambiguous episode identity permits cross-feed collisions and shared-cache poisoning

- **File:line:** `chorus/models.py:56-117`, `chorus/transcripts.py:79-85`, `chorus/transcripts.py:259-287`, `chorus/stores/postgres.py:110-125`
- **Severity:** must-fix
- **Concrete failure scenario:** `EpisodeInput` accepts multiple identity families simultaneously, while `resolved_id` prioritizes `video_id` and otherwise hashes `guid` without the feed URL. An attacker can submit a victim `video_id` together with an attacker-controlled RSS feed and GUID. If earlier providers do not resolve the video, the RSS provider returns attacker text labeled with the victim ID, and the shared cache stores it under that ID. Later requests for the victim ID receive the poisoned transcript before any provider lookup. Separately, identical GUIDs in two feeds collide because the feed URL is absent from the hash.
- **Suggested fix:** Validate that exactly one source identity family is present. For RSS, derive the cache key from a canonical feed URL plus GUID and source type. Require every provider result to match the requested cache identity before storing it, namespace cache keys by provider/source, and consider tenant scoping where callers can supply content.

### 12. The viewer forwards the Chorus bearer token to arbitrary absolute audio URLs

- **File:line:** `web/lib/api-client.ts:101-115`, `web/lib/url.ts:9-12`
- **Severity:** must-fix
- **Concrete failure scenario:** `fetchAudioObjectUrl()` passes `audioPath` through `absoluteUrl()`, which preserves any absolute HTTP(S) URL, then always adds `Authorization: Bearer <Chorus token>`. Current Vercel Blob jobs therefore send the Chorus credential to the blob origin. If a compromised or malformed API response contains an attacker-controlled audio URL, the browser sends the credential directly to that host.
- **Suggested fix:** Attach authorization only to relative URLs or URLs whose parsed origin exactly matches the configured Chorus API origin. Fetch third-party artifacts without the Chorus header, or preferably expose only same-origin authenticated download endpoints. Add tests for relative, same-origin absolute, blob-origin, and attacker-origin URLs.

### 13. The documented cross-origin viewer deployment cannot pass browser preflight

- **File:line:** `web/lib/api-client.ts:36-50`, `web/README.md:30-53`, `chorus/app.py:118-134`
- **Severity:** must-fix
- **Concrete failure scenario:** The viewer runs at its documented frontend URL and calls a separate API URL with an `Authorization` header. The browser sends an `OPTIONS` preflight. Chorus installs no CORS middleware, and the authentication middleware rejects the unauthenticated preflight with 401 when a master token is configured. Even a successful API response lacks the required cross-origin response headers, so the browser blocks it.
- **Suggested fix:** Add `CORSMiddleware` with an explicit environment-specific origin allowlist, allowed methods, and the `Authorization` header. Ensure preflight is handled before bearer enforcement. Alternatively, proxy all API and artifact requests through the Next.js origin and keep the browser same-origin. Add an integration test that exercises a real preflight and authenticated cross-origin request.

## Should-fix

### 14. LLM usage accounting is neither isolated per job nor implemented in the Inngest path

- **File:line:** `chorus/pipeline.py:117-135`, `chorus/pipeline.py:228-236`, `chorus/inngest_app.py:138-160`
- **Severity:** should-fix
- **Concrete failure scenario:** Two background jobs share one metered LLM client. Job A snapshots the global counters, job B consumes tokens, and job A computes its delta afterward, attributing some of B's usage to A. The Inngest curation loop never takes or applies a token snapshot at all, so otherwise equivalent hosted jobs report null/zero LLM usage.
- **Suggested fix:** Return usage from each LLM call and aggregate it in the job execution context instead of differencing shared mutable counters. Persist each idempotent Inngest step's usage and combine it once during finalization.

### 15. Remote transcript responses are eagerly buffered without a byte or segment limit

- **File:line:** `chorus/transcripts.py:125-168`, `chorus/transcripts.py:171-199`, `chorus/transcripts.py:229-290`, `chorus/transcripts.py:353-404`
- **Severity:** should-fix
- **Concrete failure scenario:** A caller-controlled RSS endpoint or transcript URL returns a multi-gigabyte body or an enormous JSON segment array. `httpx` buffers the response and the parser materializes the full payload before any application cap is applied, allowing memory exhaustion and, for successfully parsed content, unexpectedly large downstream LLM cost.
- **Suggested fix:** Stream responses with strict compressed and decompressed byte ceilings, reject excessive `Content-Length` early, cap feed entries and transcript segments, and enforce a maximum transcript duration and character count before caching or LLM calls.

### 16. Malformed provider payloads bypass intended fallback behavior

- **File:line:** `chorus/transcripts.py:157-168`, `chorus/transcripts.py:187-199`, `chorus/transcripts.py:385-404`, `chorus/transcripts.py:407-425`, `chorus/ingest.py:27-33`
- **Severity:** should-fix
- **Concrete failure scenario:** Supadata returns HTTP 200 with a missing `text` field, or an RSS transcript contains invalid JSON. `KeyError`, `TypeError`, or JSON decode errors escape the provider's documented `NotFound`/`ProviderError` taxonomy. A `ValueError` is swallowed by ingestion without trying the next provider, while a `KeyError` can abort the whole job rather than degrade that episode or fall back to Deepgram.
- **Suggested fix:** Validate every provider response into typed boundary models and translate decode/schema failures into explicit provider exceptions. Decide which failures are retryable and which permit fallback, and make the provider chain handle those categories consistently. Include the provider name and safe response metadata in diagnostics.

### 17. Selection requests silently discard the requested speaker profile

- **File:line:** `chorus/models.py:328-331`, `chorus/app.py:167-178`, `chorus/mcp_server.py:35-77`
- **Severity:** should-fix
- **Concrete failure scenario:** A caller sends `/digest/select` with a two-host `profile`. The route constructs a new `DigestRequest` without copying `request.profile`, so the job uses the default monologue profile. The MCP selection tool has the same omission, and the MCP submission tool exposes no profile input.
- **Suggested fix:** Copy `profile` into the constructed `DigestRequest` and expose the same profile schema through MCP. Add parity tests asserting that HTTP selection, HTTP explicit submission, and both MCP paths persist identical profiles.

### 18. Per-speaker voice IDs are accepted but never used for rendering

- **File:line:** `chorus/models.py:179-182`, `chorus/audio.py:188-215`, `chorus/audio.py:253-256`, `chorus/pipeline.py:171-183`
- **Severity:** should-fix
- **Concrete failure scenario:** A two-host request assigns distinct `voice_id` values to its speakers. The renderer is constructed once from environment configuration, and the render interface never receives the request profile. Both speakers therefore use renderer-level voices rather than the requested values, despite the API accepting and persisting them.
- **Suggested fix:** Pass the validated profile or an explicit speaker-to-voice map into audio rendering. Resolve allowed voice IDs per request, reject unavailable mappings before provider calls, and test that each dialogue speaker invokes ElevenLabs with the selected voice.

### 19. One long dialogue turn violates the renderer's own character limit

- **File:line:** `chorus/audio.py:75-78`, `chorus/audio.py:155-172`, `chorus/audio.py:202-228`
- **Severity:** should-fix
- **Concrete failure scenario:** The script returns a single valid grounded turn longer than `MAX_DIALOGUE_CHARS`. `_chunk_turns()` places that turn alone in a chunk without splitting or rejecting it, and the renderer sends the oversized request to ElevenLabs. A provider 4xx then degrades the job to `done` with no audio even though local validation could have prevented the call.
- **Suggested fix:** Enforce a per-turn limit during script parsing or split long turns on sentence boundaries while preserving speaker identity. Assert that every outbound chunk is at or below the provider limit.

### 20. Invalid monologue output becomes a successful but misleading empty digest

- **File:line:** `chorus/script.py:188-243`, `chorus/pipeline.py:240-260`
- **Severity:** should-fix
- **Concrete failure scenario:** The LLM produces no parseable or grounded `TAKE` blocks for a digest that contains highlights. `_write_takes()` silently drops every block, and `write_script()` returns the fallback line “Nothing cleared the bar.” The pipeline treats this as successful script generation and can render it, masking a model-format or grounding failure as an editorial conclusion.
- **Suggested fix:** If the digest has highlights and zero valid takes survive parsing/grounding, raise an explicit script-generation error so the existing warning/degradation path records the failure. Preserve the empty editorial response only when the digest itself has no selected highlights.

### 21. Viewer polling can continue forever after persistent server failures

- **File:line:** `web/lib/useJob.ts:45-78`, `web/lib/config.ts:9-11`
- **Severity:** should-fix
- **Concrete failure scenario:** A job remains non-terminal or the API continuously returns 500/network errors. The hook schedules fixed-interval requests indefinitely, with no backoff, elapsed-time ceiling, or retry budget. Navigation clears the timer but does not abort an in-flight fetch, causing avoidable load and potentially stale state updates.
- **Suggested fix:** Add exponential backoff with jitter, a maximum elapsed time or retry budget, and a visible retry/resume state. Use `AbortController` for each request and abort it during cleanup or when the job ID/token changes.

### 22. The request-size limit is bypassed when `Content-Length` is absent

- **File:line:** `chorus/app.py:118-134`
- **Severity:** should-fix
- **Concrete failure scenario:** A client sends a chunked or otherwise lengthless multi-gigabyte JSON request to an API route, including unauthenticated `POST /keys`. The middleware checks only a numeric `Content-Length`, so Starlette continues reading and parsing the body despite the nominal 1 MiB limit.
- **Suggested fix:** Enforce a streaming byte limit in ASGI receive handling or at the ingress proxy, while retaining the header check as an early rejection. Configure equivalent platform limits and test chunked/omitted-length bodies.

### 23. Email delivery failure creates an active key the user cannot recover

- **File:line:** `chorus/keys.py:51-69`, `chorus/app.py:136-148`
- **Severity:** should-fix
- **Concrete failure scenario:** Key issuance commits the hash, then Resend fails. The endpoint returns 500 before the caller sees the plaintext key. A retry with the same email is rejected by the one-hour issuance limit, while the unknown key remains valid in storage.
- **Suggested fix:** Model issuance as pending until delivery succeeds, or revoke the just-created key on delivery failure in a transactionally safe compensation step. Make retries idempotent without storing plaintext credentials.

### 24. The Postgres transcript-cache pool is never closed

- **File:line:** `chorus/config_env.py:87-92`, `chorus/stores/postgres.py:96-129`, `chorus/app.py:111-114`
- **Severity:** should-fix
- **Concrete failure scenario:** In Postgres mode, `build_deps()` installs a `PostgresTranscriptCache` with its own connection pool. The application lifespan closes the job store and an owned SQLite key store, but never calls the cache's `close()`. Repeated local lifespan cycles or graceful worker recycling leave cache connections open until process teardown.
- **Suggested fix:** Give the dependency bundle an async or synchronous close contract and close every owned store/cache/provider in reverse construction order during lifespan shutdown. Add a lifespan test with instrumented pools.

### 25. Non-finite JSON scores can be promoted to top-ranked highlights

- **File:line:** `chorus/llm.py:213-242`
- **Severity:** should-fix
- **Concrete failure scenario:** Python's JSON decoder accepts non-standard `NaN` and `Infinity` tokens. A model response containing `"score": NaN` passes numeric conversion; the current min/max clamp can turn the non-finite value into a boundary score rather than rejecting it, corrupting ranking and selection.
- **Suggested fix:** Reject non-standard JSON constants during decoding and require `math.isfinite(score)` before clamping. Treat any invalid score as a parse failure subject to the existing retry path.

## What is solid

- When a master token is configured, the application-level middleware also covers the mounted MCP transport and local artifact route. The MCP mount is registered last, so it does not shadow the earlier REST routes. Master-token comparison uses `secrets.compare_digest`.
- Issued API keys are stored as SHA-256 hashes rather than plaintext, lookups and job SQL use parameters, and the SQLite job store serializes access with a lock. No SQL-injection path was found in the reviewed stores.
- Local artifact names are derived through a restrictive stem validator at the production call site. No path-traversal path was found in the current render flow.
- The in-process pipeline catches unexpected top-level failures and normally writes `failed`; script and audio exceptions are deliberately converted into warnings before a final `done`. Curation checks score/window cardinality, and script parsing enforces grounding and a turn cap.
- The React viewer renders API strings through normal JSX escaping; no `dangerouslySetInnerHTML` or equivalent injection sink was found. Audio object URLs are revoked during replacement and cleanup.
- The supplied checkpoint baseline is 165 passing tests with Ruff, mypy, and the golden path green. This review could not rerun that baseline because the supplied virtual-environment launcher points to a missing Python 3.12 executable. The findings above are based on direct source inspection and narrow static tracing.
[2026-09-22T00:03:50.733Z] Turn completion inferred after the main thread finished and subagent work drained.

[2026-09-22T00:03:51.369Z] Final output
# Chorus Wave 1 Goal-Boundary Review
