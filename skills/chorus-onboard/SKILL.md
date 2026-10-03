---
name: chorus-onboard
description: Set up and run Chorus, the podcast-curation agent, for your principal. Use when the principal asks to set up Chorus, build their Chorus agent, change their Chorus settings or soul, or run their podcast digest.
---

# Chorus onboarding

Chorus serves its own instructions, so this skill is only a starting point.
They ship with Chorus and update when Chorus does.

1. If Chorus tools are not available (no `onboarding_status` tool and no
   `chorus` command), follow `INSTALL_FOR_AGENTS.md` in the Chorus repository
   (https://github.com/ctmmit/chorus) first.
2. Call `onboarding_status`. Shell-only hosts run `chorus setup status`.
3. While `ready` is false, take the principal through `next`:
   - ask them its `ask` text in plain words
   - follow its `agent_notes`
   - call the matching `onboarding_*` tool (or `chorus setup` action)
   - repeat
4. Never answer an onboarding question for the principal. The soul step is
   required: show them the full draft and save it only once they approve it.
5. Once ready, `run_my_digest` starts a digest. Poll `get_digest(job_id)`
   until it is `done` or `failed`, then share:
   - each highlight with its timestamp
   - any refusals, without padding
   - the episode file

To change a setting later, call `onboarding_options(<step>)`, then the
matching set tool. To revise the soul, call `onboarding_soul_show`, revise it
with the principal, then call `onboarding_soul_save`.
