# Install Chorus for your principal

You are an AI agent. Your principal gave you this repository and asked you to
set up Chorus for them. Chorus listens to the podcasts they can't get to and
gives back highlights, each with a timestamped quote you can check, plus a
short voiced episode. These steps work for any host that can run a command or
connect an MCP server: Claude Code, Claude Cowork / Claude Desktop, Codex,
Grok Build, Muse Code, OpenClaw, Hermes, Cursor, Gemini CLI, and others.

Follow the steps in order. Ask your principal before installing anything, and
ask them every onboarding question. Never answer one on their behalf.

## 1. Pick your tier

| Your host can... | Tier | Examples |
|---|---|---|
| launch a local MCP server (stdio) | **A: MCP** (best) | Claude Code, Claude Desktop & Cowork, Codex, Grok Build, Muse Code, OpenClaw, Hermes, Cursor, Gemini CLI |
| run shell commands, but not MCP | **B: CLI** | any terminal-capable agent |
| neither: it runs in a vendor's cloud sandbox | **C: hosted** | the Muse app, browser-only assistants |

Tier C cannot use a copy of Chorus installed on the principal's machine; skip
to [Tier C](#tier-c-hosted-chorus).

## 2. Install (tiers A and B)

Requires Python 3.12 and git. Choose a permanent location, because the MCP
config will point into it (for example `~/chorus`).

macOS / Linux:

```bash
git clone https://github.com/ctmmit/chorus.git ~/chorus
cd ~/chorus
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -e . --no-deps
```

Windows (PowerShell):

```powershell
git clone https://github.com/ctmmit/chorus.git $HOME\chorus
cd $HOME\chorus
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install -e . --no-deps
```

This puts two commands in the venv, `chorus-mcp` and `chorus`. Below,
`<CHORUS_MCP>` and `<CHORUS>` mean their absolute paths:

- macOS / Linux: `~/chorus/.venv/bin/chorus-mcp` and `~/chorus/.venv/bin/chorus`
- Windows: `%USERPROFILE%\chorus\.venv\Scripts\chorus-mcp.exe` and `chorus.exe`

The principal's settings, keys, soul, and history live in `~/.chorus/`, never
in the clone, so updating the code can't overwrite them.

## 3. Connect

### Tier A: register the MCP server

Use the entry for your host. Every entry starts the same command.

**Claude Code**

```bash
claude mcp add --scope user chorus -- <CHORUS_MCP>
```

**Claude Desktop and Claude Cowork.** Edit `claude_desktop_config.json`
(macOS: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`),
then restart the app. Desktop passes these servers through to Cowork sessions.

```json
{ "mcpServers": { "chorus": { "command": "<CHORUS_MCP>" } } }
```

**Codex.** Add to `~/.codex/config.toml`:

```toml
[mcp_servers.chorus]
command = "<CHORUS_MCP>"
```

**Grok Build**

```bash
grok mcp add chorus -- <CHORUS_MCP>
```

**OpenClaw** (`openclaw.json`)

```json5
{ mcp: { servers: { chorus: { command: "<CHORUS_MCP>" } } } }
```

**Hermes** (`~/.hermes/config.yaml`)

```yaml
mcp_servers:
  chorus:
    command: "<CHORUS_MCP>"
```

**Any other MCP host:** add a stdio server named `chorus` whose command is
`<CHORUS_MCP>`, with no arguments.

Reload your host's MCP servers. If it supports Agent Skills, also install
`skills/chorus-onboard` (and `skills/chorus`) into its skills folder: for
example `~/.claude/skills/`, `~/.codex/skills/`, or `.agents/skills/`. Then go
to step 4.

### Tier B: use the JSON CLI

Every command prints one JSON object. Errors print `{"error": ...}` and exit
with code 2.

```bash
<CHORUS> setup status                         # where setup stands + the next step
<CHORUS> setup options <step>                 # the prompt and options for a step
<CHORUS> setup set <step> <value>             # record a choice
<CHORUS> setup key <ENV_NAME>                 # reads the key from stdin
<CHORUS> setup soul-draft --source interview --answers answers.json
<CHORUS> setup soul-save <name> --file soul.md
<CHORUS> setup shows --show "<catalog show>" --feed <rss-url> [--weekly]
<CHORUS> setup smoke [--skip]
<CHORUS> setup run [--episode <id>]
```

## 4. Onboard

Call `onboarding_status` (Tier B: `chorus setup status`). While `ready` is
false, it returns `next`, the step to take the principal through:

- `ask`: what to ask them, in plain words
- `options`: the choices, each with a `value`. Options with `available: false`
  are on the roadmap; mention them, but don't offer them.
- `agent_notes`: how to handle this step
- `data`: anything else you need (interview questions, required keys, catalog shows)

Ask, call the matching tool, and repeat. The steps are:

1. **mode**: does Chorus run on its own on this machine, or through you
2. **brain**: who does the thinking. The options are:
   - `host`: you, the agent, score the segments and write the script with your own
     model. No Anthropic key is needed, and Chorus checks every citation.
   - `anthropic`: Chorus calls Claude itself, using an Anthropic API key.
   - `mock`: the free demo.
3. **voice**: who voices the episode. The options are:
   - `host-plugin`: your own text-to-speech tool, such as an ElevenLabs MCP server,
     connector or plugin, billed to the principal's account. If you don't have one,
     you can offer to install the official server: add a stdio MCP server running
     `uvx elevenlabs-mcp` with `ELEVENLABS_API_KEY` in its env, and have the
     principal enter that key into your config themselves.
   - `elevenlabs-key`: Chorus calls ElevenLabs itself. Two-host episodes sound more
     natural this way.
   - `text-only`: no audio.
4. **transcripts**: where transcripts come from
5. **keys**: whatever API keys those choices need. A key pasted into chat stays
   in the conversation transcript. Tell the principal, and offer the
   alternative of adding `ENV=value` lines to `~/.chorus/.env` themselves.
6. **soul** (required): the lens Chorus curates through. You can interview
   them, draft it from what you know about them, derive it from their notes, or
   start from a preset. **Show them the full draft and save it only once they
   approve.**
7. **shows**: catalog shows and/or RSS feeds, and whether to run weekly
8. **updates**: notify, automatic, or off
9. **smoke_test**: one test digest on a bundled sample. Ask first if it's billed.

Chorus won't run a digest until the soul is saved and validated.

## 5. Run

Tier A: `run_my_digest` returns a `job_id`, a `brain`, and `drive: "host_next"`
whenever you do part of the work (your model thinks, your voice tool renders,
or both).

- **With `drive`, loop on `host_next(job_id)`** and do what each task says:
  - `wait`: call `host_next` again after `wait_seconds`.
  - `score`: score the episode's windows against the lens, following the
    task's `instructions`, then call `host_submit_scores`. If you can run
    subagents, give each episode in `pending_episodes` to its own subagent;
    `host_episode` returns any one episode's windows.
  - `script`: write the takes (plus turns for a two-host episode), each citing
    a highlight by `ref`, then call `host_submit_script`.
  - `render`: voice each chunk in `render_plan` with your text-to-speech tool,
    using its `voice_id` and MP3 output. Then call `host_submit_audio` with
    `[{"index": i, "path": "<file>"}]`, or `"base64"` instead of `"path"`. If you
    can't voice it, pass `skip_reason`; the digest still goes out as text.
  - `done`: deliver `result`.

  Pass `agent_model` (the model you are) to `run_my_digest`, so the run
  records who judged it.
- **Without `drive`,** poll `get_digest(job_id)` until it reports `done` or `failed`.

Tier B: `chorus setup run` waits and prints the result. With the host brain,
or the agent voice, use `chorus setup host-start`, `host-next`, `host-scores`,
`host-script` and `host-audio` instead.

Chorus never takes a quote from you. It cuts every quote from the
transcript, so highlights stay verifiable whichever model scored them.

Give the principal:

- each highlight with its quote and timestamp
- any episode that was refused ("nothing cleared the relevance bar"). Don't
  pad the digest with weaker material.
- the episode file path

## Tier C: hosted Chorus

Cloud-sandboxed agents connect to a hosted Chorus instance over HTTPS instead
of a local install:

- the remote MCP endpoint `https://<chorus-host>/mcp`, or
- the HTTP API (`SKILL.md` has the full contract), with a bearer token or a
  self-serve key from `POST /keys`

Hosted Chorus has no onboarding tools yet. Run the soul interview yourself
using `skills/chorus-soul-bootstrap/SKILL.md`, show the draft to your principal,
keep the approved soul, and pass it in every `submit_digest` call.

## Updating

Run `git pull` in the clone, then
`.venv/bin/python -m pip install -r requirements.txt` (Windows:
`.venv\Scripts\python.exe -m pip install -r requirements.txt`), then restart
your MCP host. The principal's state in `~/.chorus/` is not touched.
