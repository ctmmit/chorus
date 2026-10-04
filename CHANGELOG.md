# Changelog

Chorus follows [semantic versioning](https://semver.org). Before 1.0, a minor
bump (0.2 → 0.3) may break things and a patch bump (0.2.0 → 0.2.1) will not.
`chorus update` and `onboarding_status` treat it that way: a breaking update
always asks first, even under the automatic update policy.

Each release is a `vX.Y.Z` tag. The release workflow publishes it to PyPI as
`chorus-agent` and to GitHub Releases, using the section below that matches
the version.

## Unreleased

## 0.3.0

Chorus delivers to the podcast app, learns from what the principal keeps,
and hears the conversation between sources across weeks. Built phase by
phase against `docs/PLAN_0.3.md` and its goal function
(`scripts/plan_goal.py`).

- **Chapters and source links in the episode.** The MP3 now carries ID3v2.4
  chapters, one per outline segment (or per source for host-mode scripts),
  so podcast players show the episode's structure. Each chapter links to the
  first source moment it cites, opening YouTube, Spotify or a direct audio
  file at that timestamp. The same list is on the job as `chapters`, and
  every digest episode now carries its source `url`.
- **A private podcast feed.** Every finished digest with audio now arrives
  in the principal's podcast app. `GET /feed` or the `get_podcast_feed` tool
  returns a feed URL to add once. It carries the MP3, show notes with
  timestamped source links, chapters and the transcript, and is authorized by
  an HMAC token in the URL because podcast apps cannot send headers.
  `chorus feed` writes a local `~/.chorus/feed.xml` for desktop players.
- **Curation has a number.** `scripts/eval_curation.py` curates labelled
  cases (`fixtures/evals/cases.json`: a soul, a transcript, and the windows a
  careful reader with that soul would and would never surface) and reports
  precision, must-recall, never-hits and refusal accuracy. It fails when a
  case drops below the committed mock baseline, and `--live` runs the real
  scorer for before-and-after numbers on prompt changes.
- **Ratings teach the lens, with approval.** Every highlight has a stable
  `highlight_id` and can be rated up or down, with a note, through
  `rate_highlight`, `POST /feedback`, or the signed "More like this" / "Less
  like this" links now in each digest email. With 8 or more ratings,
  `propose_soul_update` proposes edits to the soul's topic lists, each with
  its evidence; `apply_soul_update` applies only the ones the principal
  accepts and records `soul_origin: feedback:<proposal_id>`. Subscriptions
  now carry `soul_origin`.
- **Threads across sources.** After curation, Chorus finds the questions
  two or more sources spoke to this week and how each answered (`agrees`,
  `disagrees`, `adds`), on `digest.threads`, disagreements first. Every
  member is a surfaced highlight and every thread spans at least two
  sources. The outline writer is offered them as segments that put sources
  in conversation, and the digest email leads with them. A synthetic
  rebuttal transcript (`sample_counter`) joins the public fixtures.
- **Memory across weeks.** Each finished digest remembers the claims it
  surfaced. Later runs demote a window that repeats one from the last four
  weeks (`REPEAT_PENALTY`, with the reason saying so), and threads can bring
  in a dated claim from an earlier digest. `remember: false` on a request or
  subscription keeps a run out of memory, and `clear_memory` forgets it all.
- **Structured context.** A digest request may carry `context_blocks`
  (source, items, as-of date) next to the `context` string. Chorus renders
  them into the context within the same budget, each source guaranteed a
  share, and records `usage.context_sources`. The new `chorus-context` skill
  gives the principal's agent recipes for building them from reading,
  projects, calendar and tasks, and a list of what never to send.
  `ContextProvider` (with a Readwise implementation) is the seam for Chorus
  to pull a source itself later.
- **Ask the episode.** `POST /digest/{job_id}/ask` and the `ask_digest`
  tool answer a question from that digest's transcripts only. Every sentence
  quotes the transcript at a timestamp, a sentence that cites anything else is
  dropped, and when nothing speaks to the question the answer is refused.
- **Brief me now.** `POST /quick-take` and the `quick_take` tool judge one
  episode in about a minute: `listen`, `skim` or `skip` by a fixed rule on
  the scores, up to three reasons each citing a moment, and a 60 to 90 second
  take (voiced on request, with a chapter per reason). A shared link with
  `mode: "quick"` gets a take straight from the share sheet.
- **Personas as sources.** A persona's owner can publish finished digests to
  it; a subscription can listen to a public persona (`kind: "persona"`) and
  hear the episodes it surfaced, curated through the subscriber's own soul and
  cited to the primary source. Up-votes on those highlights endorse the
  persona; `/network` shows the counts, and each persona has a public podcast
  feed advertised on its A2A card.
- **The writer picks its sources.** An episode is no longer limited to the
  three best-scored sources with a fixed walkthrough each. Every source with
  highlights (up to 12, the brief budget) is a candidate, and the writer
  decides what gets airtime, how much, and in what order. With no
  `target_minutes` set, it also chooses the length, from 4 to 15 minutes. The
  remaining rules are about being easy to follow by ear (an intro, a close,
  each source introduced the first time it comes up) and about grounding.
  Host mode gives the principal's agent the same latitude.

## 0.2.0

The principal's own agent can install, set up, and run Chorus.

- **Onboarding.** `chorus onboard` for a person in a terminal, and the same
  steps for any agent: `onboarding_*` tools on the local MCP server, or
  `chorus setup` JSON commands for shell-only hosts. The steps are mode,
  brain, voice, transcripts, keys, voices, soul (required), shows, updates,
  and a smoke test. `INSTALL_FOR_AGENTS.md` is the entry point for an agent handed
  the repository link.
- **Choose your voices.** The optional voices step lists the voices on the
  principal's ElevenLabs account (or asks for a voice id when the key cannot
  list them) and records a host voice and a co-host voice for two-host
  episodes. Chorus's own renderer and the agent's voice tool both use them;
  unchosen voices fall back to `ELEVENLABS_VOICE_ID` /
  `ELEVENLABS_COHOST_VOICE_ID`, then the defaults.
- **Your agent as the brain** (`brain = host`). The agent scores transcript
  windows and writes the script with its own model, driven by `host_next`.
  Chorus cuts every quote from the transcript and drops script beats that
  cite no highlight, so grounding holds for any model. Runs record `brain`,
  `brain_model` and `rubric_version`.
- **Your agent's voice tool** (`voice = host-plugin`). Chorus sends a render
  plan of voice-tagged chunks; the agent voices them with its own
  text-to-speech tool (the ElevenLabs MCP server, a connector or plugin), and
  Chorus checks they are MP3 and joins them. It works with any brain.
- **Local state in `~/.chorus/`** (`$CHORUS_HOME`): config, souls, keys, job
  history and audio. Existing `chorus.db` and `.env.local` are copied there
  on first run.
- **Packaging and updates.** Chorus is installable as the `chorus-agent`
  distribution (wheel or `uvx`), with the presets, demo catalog and sample
  transcript bundled. `chorus update` updates the install, `chorus_version`
  reports new releases, and config migrations run on start with a backup.
- **Plugins and registration.** One-command installs for Claude Code
  (`.claude-plugin/`) and Codex (`plugins/codex/chorus`, listed in
  `.agents/plugins/marketplace.json`). `chorus register` connects Chorus to
  Claude Code, Claude Desktop/Cowork, Codex and Grok Build, showing each
  change and backing up any config file first.
- **Weekly digests.** `chorus schedule on|off|status` uses Task Scheduler,
  launchd or cron when Chorus does all the work itself. When the agent is the
  brain or the voice, onboarding asks the agent to schedule the run in its own
  scheduler instead. Every `chorus run` writes `~/.chorus/digests/<date>.md`.
- **Fix.** On Windows, a background run could stall in `wait` when the agent
  polled while state was being saved.

## 0.1.0

The hosted service and local MCP server. Persona-conditioned, timestamp-grounded
highlights and a voiced episode; two-host dialogue; subscriptions and the
weekly email; the Next.js viewer; A2A discovery.
