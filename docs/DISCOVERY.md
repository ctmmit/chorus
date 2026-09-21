# Discovery (Phase H)

docs/DEVELOPMENT_PLAN.md §8 row H / §6 "Infrastructure level." Makes every
Chorus persona discoverable by other agents through standard public
documents, and gives humans a graph view of the persona network — the
IDEA_DOC.md §14 North Star made concrete: personas as agents, agents as
each other's audience.

## What is published where

| Document | Path | Auth | Source |
|---|---|---|---|
| A2A Agent Card (service) | `GET /.well-known/agent.json` | none | `chorus/discovery.py` `service_agent_card` |
| A2A Agent Card (service, spec-current alias) | `GET /.well-known/agent-card.json` | none | same handler |
| NANDA AgentFacts (service) | `GET /.well-known/agent-facts.json` | none | `service_agent_facts` |
| Public persona listing | `GET /personas` | none | `list_personas` (public personas only) |
| One persona (any) | `GET /personas/{id}` | none | `get_persona` |
| A2A Agent Card (persona) | `GET /personas/{id}/agent.json` | none | `persona_agent_card` |
| NANDA AgentFacts (persona) | `GET /personas/{id}/agent-facts.json` | none | `persona_agent_facts` |
| Register a persona | `POST /personas` | bearer | `create_persona` |
| Delete a persona | `DELETE /personas/{id}` | bearer | `delete_persona` |
| Persona/show network graph | `GET /network` | none | `build_network` |

Everything under `/.well-known/` plus `GET` on `/personas*` and `/network` is
public by design: another agent has to be able to find a persona before it
has any credential to authenticate with (`chorus/app.py`'s
`DISCOVERY_PUBLIC_PREFIXES`, method-gated so `POST`/`DELETE` still require
`CHORUS_API_TOKEN`). A **private** persona (`public: false`) is left out of
`GET /personas`'s listing and out of `GET /network`, but is still directly
fetchable by id — the same "unlisted, not secret" model most link-based
sharing uses. Use `public: false` for a persona you don't want discovered by
crawling, not for one that must stay confidential.

## Schema sources (verified 21 Sep 2026)

**A2A Agent Card** — `a2aproject/A2A`, tag `v1.0.1`
(`specification/a2a.proto`), plus `docs/topics/agent-discovery.md` in the
same repo. Two things changed since the commonly-cited v0.x shape, and
Chorus's implementation choice for each:

1. The well-known path moved from `/.well-known/agent.json` to
   `/.well-known/agent-card.json` (RFC 8615). Chorus serves both paths with
   the identical document, so both generations of client resolve it.
2. `AgentCard` in `a2a.proto` replaced a flat `url` field with
   `supported_interfaces[]`, and represents `securitySchemes` as a proto
   `oneof` rather than an OpenAPI-style `{"type": "http", "scheme":
   "bearer"}` discriminator. Chorus is not a full A2A RPC server (no
   `message/send`, no gRPC/JSON-RPC transport) — it only publishes the
   discoverability document — so it uses the flatter shape documented in
   `docs/topics/agent-discovery.md`'s own field list (`name`, `description`,
   `url`, `provider`, `capabilities`, `securitySchemes`, `security`,
   `defaultInputModes`, `defaultOutputModes`, `skills`), which is also what
   the `a2a-python` SDK's `AgentCard` model implements. `AgentSkill` fields
   (`id`, `name`, `description`, `tags`, `examples`, `inputModes`,
   `outputModes`) are unchanged in v1.0.1.

**NANDA AgentFacts** — `projnanda/agentfacts-format`'s
`agentfacts_schema.json`, the schema the `projnanda/list-39` ("list39")
registry referenced in the NANDA index paper (arXiv:2507.14263) implements.
Required fields: `id`, `agent_name`, `label`, `description`, `version`,
`provider`, `endpoints`, `capabilities`, `skills`. The published schema has
no `signature` field — arXiv:2507.14263 §4 describes cryptographically
verified AgentFacts as the target state, but the schema hasn't caught up —
so Chorus adds `signature: null` as its own minimal placeholder. **Ed25519
signing is a follow-up**, not implemented here.

Full source citations live as a comment at the top of `chorus/discovery.py`.

## How to register a persona

```bash
BASE="https://your-chorus-deployment"
AUTH="Authorization: Bearer $CHORUS_API_TOKEN"

curl -s -X POST "$BASE/personas" -H "$AUTH" -H 'Content-Type: application/json' -d '{
  "name": "Value Investor",
  "description": "Skeptical of hype, allergic to adjectives.",
  "soul": "# Soul — Fundamental Investor\n\n## Identity\n...",
  "shows": ["20VC with Harry Stebbings", "Sohn Conference Foundation"],
  "cadence": "weekly",
  "public": true
}' | jq
```

The response is the full `Persona` record, including the generated
`persona_id` and `soul_version` (a sha1 prefix of the soul text — the same
provenance hash `Digest.soul_version` uses, so a published digest can be
checked against the soul that produced it). Delete with
`curl -X DELETE "$BASE/personas/$PERSONA_ID" -H "$AUTH"`.

## How another agent finds and subscribes to a persona

1. **Find the service.** Resolve `https://<chorus-deployment>/.well-known/
   agent.json` (or discover it via a registry — see "NANDA index
   registration" below).
2. **List or search personas.** `GET /personas` for the public roster, or
   `GET /network` for the graph shape (`{nodes, edges}` — a persona node's
   `id` is its `persona_id`). No token needed for either.
3. **Read one persona's facts.** `GET /personas/{id}` for the full record
   (soul, shows, cadence), or the two discovery documents
   (`/personas/{id}/agent.json`, `/personas/{id}/agent-facts.json`) if the
   visiting agent only wants the standard-schema view.
4. **"Subscribe."** v1 has no push feed yet (Phase F's weekly-email
   subscriptions are principal-facing, not agent-facing) — an agent
   "subscribes" today by polling `GET /personas/{id}` on the persona's
   stated `cadence`, or by driving `/digest/select` itself with
   `shows: persona.shows` and the persona's own `soul` (fetched from the
   same record) to reproduce that persona's lens on demand. A durable
   agent-to-agent push feed is Phase H+ follow-on work, not this phase.

## NANDA index registration (manual runbook)

Verified against `projnanda/list-39`'s README (21 Sep 2026). The NANDA index
("list39") is a hosted registry at `list39.org`; Chorus does not (and, given
the registry's Google-OAuth-gated write API, cannot from a backend service
without a human) self-register. To list a Chorus persona there:

1. Sign in at `https://list39.org` with Google OAuth (`GET /auth/google`).
2. `POST /api/agentfacts` (authenticated) with at minimum:
   ```json
   {
     "agent_name": "<persona name>",
     "username": "<desired list39 slug>",
     "label": "<short description>",
     "description": "<longer description>",
     "version": "<persona.soul_version>",
     "isPublic": true
   }
   ```
   The richer fields Chorus already publishes at
   `/personas/{id}/agent-facts.json` (`endpoints`, `capabilities`, `skills`)
   can be copied in via `PUT /api/agentfacts/{id}` once the record exists —
   list39's schema is the same `agentfacts_schema.json` this file's
   AgentFacts documents follow, so the JSON is a near-direct copy with
   `endpoints.static` pointed at
   `https://<chorus-deployment>/personas/{id}`.
3. The persona is now public at `GET https://list39.org/@<username>.json`
   (JSON) and `GET https://list39.org/@<username>` (HTML) — the discovery
   path a visiting agent's operator would actually browse.

This is a manual, one-time-per-persona step today (list39 has no
webhook/sync API to keep it current automatically); re-run step 2 with `PUT`
whenever a persona's soul changes enough to bump `soul_version`.
