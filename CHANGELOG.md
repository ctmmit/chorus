# Changelog

Chorus follows [semantic versioning](https://semver.org). Before 1.0, a minor
bump (0.2 → 0.3) may break things and a patch bump (0.2.0 → 0.2.1) will not.
`chorus update` and `onboarding_status` treat it that way: a breaking update
always asks first, even under the automatic update policy.

Each release is a `vX.Y.Z` tag. The release workflow publishes it to PyPI as
`chorus-agent` and to GitHub Releases, using the section below that matches
the version.

## 0.2.0

The principal's own agent can install, set up, and run Chorus.

- **Onboarding.** `chorus onboard` for a person in a terminal, and the same
  steps for any agent: `onboarding_*` tools on the local MCP server, or
  `chorus setup` JSON commands for shell-only hosts. The steps are mode,
  brain, voice, transcripts, keys, soul (required), shows, updates, and a
  smoke test. `INSTALL_FOR_AGENTS.md` is the entry point for an agent handed
  the repository link.
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
