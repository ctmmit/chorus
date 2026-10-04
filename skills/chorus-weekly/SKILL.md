---
name: chorus-weekly
description: Set up and maintain a recurring weekly (or daily) Chorus podcast digest for a principal. Use when an agent finds shows for a principal, creates or manages a subscription, refreshes its context, or handles delivery/unsubscribe.
---

# Run Chorus every week

Chorus owns the schedule. Create a **subscription** once and the service
checks each of the principal's shows for new episodes on every run, digests
only the ones it has not sent before, and emails the result. There is no
gather-submit-poll loop to run yourself. This skill drives the MCP tools; the
`chorus` skill documents the same operations as HTTP routes and the
digest/poll contract.

The flow is `search_podcasts` -> `preview_subscription` -> `subscribe`.

## 1. Find the shows

Ask the principal which shows they follow, then turn each name into a source.

- `search_podcasts(query, limit=10)` searches Apple's directory and returns
  `title`, `author`, `feed_url`, `artwork_url` and `apple_id`. Show the top
  matches and let the principal confirm which one they mean; names collide.
  The source is `{"kind": "rss", "feed_url": <feed_url>, "title": <title>}`.
- `resolve_podcast(url)` takes a link the principal pasted (an RSS feed, an
  Apple Podcasts show page, or a YouTube channel at `/channel/UC...`) and
  returns the source object directly. A YouTube `@handle` link resolves only
  if the server has a YouTube API key; if it refuses, ask for the
  `/channel/UC...` URL instead of guessing.

Shows are checked through their RSS feed, so any show with a public feed
works. A YouTube channel is a source too, for shows that live only there.

## 2. Preview what the first run would send

`preview_subscription(sources, lookback_days=7, max_episodes_per_run=8)`
returns the episodes the first run would digest (published within
`lookback_days`, newest first, at most `max_episodes_per_run` shared
round-robin across shows) and an `errors` list for any source that could not
be read. Show the principal the titles. Drop or replace a source that
errored; a feed that fails here will fail on the schedule too. If the list is
empty for a show they love, the show may simply be between seasons, which is
fine to subscribe to.

## 3. Subscribe

```
subscribe(
  email="principal@example.com",
  soul="<markdown: the approved lens>",
  context="<current projects, reading, priorities>",
  sources=[...],
  cadence="weekly",          # Friday 13:00 UTC; "daily" is 13:00 UTC
  highlight_count=4,
  max_episodes_per_run=8,
)
```

Use the principal's approved `soul.md` (build one with the
`chorus-soul-bootstrap` skill if they have none). The return value is the
stored subscription; keep its `subscription_id`, you need it for everything
below.

On each run Chorus lists every source's episodes published since the last run,
drops any whose id is in `seen_episode_ids`, takes up to
`max_episodes_per_run`, and emails highlights grouped by show with a deep link
per highlight. A week with nothing new sends a short "Nothing new from your
shows this week" note listing the sources it checked (turn it off with
`notify_when_empty=False`). A source that cannot be read is named in the
email's "Could not check" footer rather than failing the run, and a failed
digest job emails a short failure notice and retries the same episodes next
run.

## Keep it current

- `list_subscriptions()` shows each subscription's sources, schedule,
  `last_run_summary` (`ran_at`, `new_episodes`, `job_id`, `skipped_reason`)
  and the episode ids already sent. Read `last_run_summary` before telling
  the principal what they received; `skipped_reason: "no new episodes"` means
  a quiet week, not an error.
- `update_subscription(subscription_id, ...)` changes only the fields you pass:
  `context` (refresh it before each week's run, or whenever the principal's
  projects and reading shift), `active` (False pauses, True resumes),
  `cadence`, `sources`, `highlight_count`, `max_episodes_per_run`,
  `notify_when_empty`. Replacing `sources` keeps the sent-episode memory, so
  nothing repeats. Do not rewrite the soul silently; a changed lens is a
  deliberate decision for the principal.
- `unsubscribe(subscription_id)` deletes it. When the principal wants fewer
  emails or to drop one show, prefer `update_subscription` (fewer sources, or
  `active=False`) over deleting unless they ask for it gone.

Every delivered email also carries a one-click unsubscribe link and the
standard `List-Unsubscribe` headers, so the principal can stop it themselves.

## Teach the lens from ratings

Each highlight in the digest email has "More like this" and "Less like this"
links, and you can record the principal's reaction yourself with
`rate_highlight(job_id, highlight_id, vote, note)` (`vote` is `up` or `down`;
put their words in `note`, such as "too much fundraising gossip").

Once there are enough ratings (8 or more), call
`propose_soul_update(subscription_id=...)`. Show the principal every proposed
edit with its `evidence`, ask which to keep, and call
`apply_soul_update(proposal_id, accept=[...], subscription_id=...)` with only
the indexes they accept. Never apply an edit they did not approve. The
subscription's later digests report `soul_origin: feedback:<proposal_id>`.

## Over HTTP

Without MCP, the same flow is `GET /podcasts/search`, `POST /podcasts/resolve`,
`POST /subscriptions/preview`, `POST /subscriptions`, `GET /subscriptions`,
`PATCH /subscriptions/{id}` and `DELETE /subscriptions/{id}`, documented in the
`chorus` skill's Subscriptions section. `POST /subscriptions/{id}/run` runs a
subscription immediately and returns `{"job_id", "skipped_reason"}`; `job_id`
is null when nothing was new.
