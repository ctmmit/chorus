---
name: chorus-context
description: Assemble the principal's current context (reading, active projects, this week's meetings, open tasks) from sources the agent can already reach, and send it to Chorus as structured context blocks. Use before submitting a Chorus digest or refreshing a subscription's context.
---

# Give Chorus this week's context

Chorus curates through two lenses: the **soul** (who the principal is, which
changes slowly) and the **context** (what they are working on and reading this
week, which changes every week). A digest with an empty context still works,
but the same soul with a fresh context surfaces noticeably better moments.

You already hold the connections that matter: the principal's reading app,
notes, calendar and task list. Chorus never needs those credentials. Pull a
small, recent slice from each source you can reach, shape it into
`context_blocks`, and send them with the digest.

## The shape

```json
"context_blocks": [
  {
    "source": "Readwise highlights",
    "as_of": "2026-10-04T08:00:00Z",
    "items": [
      {"text": "Inference cost per token fell 10x in 18 months.", "label": "The Economics of AI"},
      {"text": "Distribution beats product when switching costs are low.", "label": "Stratechery"}
    ]
  },
  {
    "source": "Active projects",
    "items": [{"text": "Fulcrum: research agent for fundamental analysts, beta this month"}]
  }
]
```

Send it on `POST /digest` or the `submit_digest` MCP tool next to (or instead of)
the `context` string. Chorus renders the blocks into `context` within the same
size budget, gives each source a fair share of it, and records which sources
made it in on the job (`usage.context_sources`). At most 10 blocks of 100
items each; an item is at most 2,000 characters, and shorter is better.

## Recipes

Use the ones you can reach. Skip a source rather than guess at it.

**Reading (Readwise, Reader, Kindle, Pocket, saved articles).** Highlights and
saved documents from the last 7 days, newest first, up to 25 items. Put the
highlight in `text` and the book or article title in `label`. With the
Readwise MCP server: `readwise_list_highlights` filtered to the last week, or
`reader_list_documents` in the `later` and `shortlist` locations.

**Active projects (Obsidian, Notion, a projects folder).** Notes for projects
the principal is actively working on: in a PARA vault, the `Projects` folder;
otherwise notes modified in the last 14 days that describe a goal. One item
per project: its name and a one-line goal or current question. Up to 10.

**This week's calendar.** Meeting titles for the next 7 days that name a
company, topic or decision ("Board prep: pricing", "Call with Acme on
procurement"). Titles only. Up to 15.

**Open tasks.** Tasks due in the next 7 days or marked high priority, titles
only. Up to 15.

**What the principal said.** Anything they told you this week about what they
are thinking about. One block, source `"Principal, in conversation"`.

## Never include

- Credentials, tokens, account numbers, or anything that looks like them.
- Private messages, email bodies, or chat content.
- Other people's personal details: attendee names and emails, phone numbers.
- Health, legal or financial-account information about anyone.
- Whole documents. Context is a list of short signals, not a corpus.

If the principal asked you not to share a source, leave it out entirely. When
unsure whether an item is sensitive, leave it out; the digest will still work.

## Local installs

With `READWISE_TOKEN` in `~/.chorus/.env.local`, `chorus run` and the weekly
schedule pull the past week's Readwise highlights into the context on their
own. The token stays on the principal's machine; the hosted service never
holds it. A source that fails is noted on the job and the digest still runs.

## Subscriptions

A subscription stores a context string, not blocks. To refresh it, render
the blocks the same way yourself (a `## <source>` heading and one `- ` line
per item) and call `update_subscription(subscription_id, context=...)` before
the next run, ideally on the same weekly cadence.
