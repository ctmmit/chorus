---
name: chorus-weekly
description: Schedule and deliver a weekly Chorus podcast digest for a principal. Use when an agent manages recurring subscriptions, context refresh, digest polling, and delivery.
---

# Run Chorus every week

Maintain the principal's podcast subscriptions in your own scheduler or agent
memory. Chorus accepts the resulting episode selection; it does not own the
schedule.

On the principal's chosen weekly cadence:

1. Gather episodes published since the previous successful run from every
   subscribed show. Prefer stable YouTube `video_id` values; URLs also work.
   Deduplicate episodes and keep at most 25 per request.
2. Refresh `context` from the principal's current projects, reading, saved notes,
   and unresolved questions. Use the latest approved `soul.md`; do not rewrite
   the soul silently.
3. Submit with MCP `submit_digest`, or use `submit_selection` when the episodes
   are in Chorus's catalog. Store the returned `job_id` with the run date.
4. Poll `get_digest(job_id)` with bounded backoff until `done` or `failed`.
   Deliver `digest_ready` text early only when the principal prefers speed over
   waiting for audio. Never retry a failed job blindly; surface its `error`.
5. Deliver the highlights, source timestamps, warnings, and authenticated
   `audio_url` through the principal's preferred channel. Record the terminal
   job id and episode ids so next week's gather does not repeat them.

If no new subscribed episodes exist, send nothing. If one episode lacks a
transcript, deliver the remaining digest and report the skipped episode.
