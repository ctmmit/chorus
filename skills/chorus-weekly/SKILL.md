---
name: chorus-weekly
description: Set up and maintain a recurring weekly (or daily) Chorus podcast digest for a principal. Use when an agent creates or manages a subscription, refreshes its context, or handles delivery/unsubscribe.
---

# Run Chorus every week

Chorus owns the schedule now (docs/DEVELOPMENT_PLAN.md §3): create a
**subscription** once and the service submits the digest job, waits for it,
and emails the result on its own — `chorus-weekly` describing "your own
scheduler or agent memory" running a manual gather-submit-poll loop is
obsolete. Use the `chorus` skill's base URL, auth, and `POST /digest` /
`GET /digest/{job_id}` contract for everything except the subscription
lifecycle itself, which lives here.

## Create the subscription

`POST /subscriptions` once, with the same `soul`/`context`/`episodes`
(or `shows`) shape as `POST /digest`, plus `email` and `cadence`:

```json
{
  "email": "principal@example.com",
  "soul": "<markdown: your lens>",
  "context": "<current projects, reading, priorities>",
  "shows": ["20VC with Harry Stebbings", "The Tim Ferriss Show"],
  "cadence": "weekly",
  "highlight_count": 4
}
```

Prefer `shows` (catalog names, resolved fresh every run — a newly published
episode is picked up automatically) over an explicit `episodes` list unless
the principal wants a fixed set. `cadence: "weekly"` runs the next Friday
13:00 UTC; `"daily"` runs the next 13:00 UTC. Store the returned
`subscription_id` — you need it for every operation below.

## Keep context fresh

The `context` sent at creation goes stale. Before each week's run (or
whenever the principal's projects/reading shift), `PATCH /subscriptions/{id}`:

```bash
curl -s -X PATCH "$BASE/subscriptions/$SUB_ID" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"context":"<this week'"'"'s projects, reading, priorities>"}'
```

Use the latest approved `soul.md`; do not rewrite the soul silently — if the
lens itself changed, that is a deliberate `PATCH` with the new `soul`, not an
automatic side effect of a context refresh. The same route pauses
(`{"active": false}`) or resumes (`{"active": true}`) delivery without
losing the subscription, and can change `cadence` or the `episodes`/`shows`
list.

## Delivery

Nothing to poll: on schedule, Chorus submits the job, waits for a terminal
state, and emails the principal directly — highlights grouped by episode
with the why-surfaced line and a working deep link per highlight, refused
and skipped episodes listed honestly, one link to the rendered audio
episode, and the lens's provenance. A failed run still emails a short
failure notice and still advances the schedule; nothing is retried blindly.

If the principal wants this week's digest immediately rather than waiting
for the schedule, `POST /subscriptions/{id}/run` — same delivery, run now,
returns `{"job_id"}` you can also poll directly with `GET /digest/{job_id}`
if you want the text before the email lands.

## Unsubscribe

Every delivered email carries a one-click unsubscribe link
(`GET /subscriptions/{id}/unsubscribe?token=...`) and the standard
`List-Unsubscribe` headers. An agent acting for the principal can also just
`DELETE /subscriptions/{id}` (or pause it with `PATCH {"active": false}`)
through the API instead of waiting for the principal to click the link.

If the principal cancels a subscribed show or wants fewer emails, prefer
`PATCH` (reduce `episodes`/`shows`, or pause) over `DELETE` unless they
explicitly want it gone.
