---
name: chorus-soul-bootstrap
description: Interview a principal and write the soul.md lens Chorus uses for podcast curation. Use when a principal has no existing persona document or reading corpus.
---

# Build a Chorus soul from an interview

Ask the principal these six questions. Preserve their wording where it carries
taste or judgment.

1. `identity`: What is your role, and what decisions are you responsible for?
2. `interests`: Which topics or questions are you actively pursuing? Ask for a
   comma-separated list.
3. `triggers`: What would make you stop and save a podcast segment? Ask for a
   comma-separated list of specific ideas, claims, people, or evidence types.
4. `ignore`: What subjects, tropes, or levels of discussion should Chorus skip?
   Ask for a comma-separated list.
5. `style`: What intellectual style do you value: empirical, contrarian,
   technical, practical, narrative, or something else?
6. `guidance`: Calibrate the bar. When should Chorus surface a segment, and when
   should it refuse rather than pad the digest?

Pass the answers to the MCP tool `build_soul_from_interview`, or write `soul.md`
yourself with exactly this template (the same schema as `chorus/bootstrap.py`):

```markdown
# Soul (interview)

## Identity & Role
<identity answer, or "Stated by the principal.">

## Core Interests
- <one bullet per comma-separated interest>

## Attention Triggers
- <one bullet per comma-separated trigger; fall back to interests>

## Anti-interests
- <one bullet per comma-separated ignore item; use "(none inferred)" if empty>

## Taste & Sensibility
<style answer, or "As stated.">

## Curation Guidance
<guidance answer, or "Surface segments matching the triggers; high bar for everything else.">
```

Read the result back once. Resolve contradictions by asking one focused follow-up.
Do not invent preferences. Save the final markdown as the principal's `soul.md`.
