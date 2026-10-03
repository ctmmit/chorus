# Chorus

> **Setting Chorus up for someone?** If you are an AI agent (Claude Code,
> Claude Cowork, Codex, Grok Build, Muse Code, OpenClaw, Hermes, or any
> other) and your principal handed you this repository, follow
> [`INSTALL_FOR_AGENTS.md`](INSTALL_FOR_AGENTS.md). If you are a person
> installing it yourself, clone it, install it (step 2 below), and run
> `chorus onboard`.

## Build a Chorus agent for your principal

You are the agent. Complete these steps in order:

1. Get a token. Ask the operator for `CHORUS_API_TOKEN`, or request your own and
   retrieve it from email:

   ```bash
   curl -i -X POST "https://<chorus-host>/keys" \
     -H "Content-Type: application/json" \
     -d '{"email":"principal@example.com"}'
   ```

2. Connect to Chorus. There are two ways, and they differ in where the work runs.

   **Hosted (an operator runs Chorus for you).** Install the skill, which teaches
   you the HTTP contract, and point it at the operator's base URL with your token:

   ```bash
   npx skills add ctmmit/chorus
   ```

   Or connect the hosted MCP endpoint directly:

   ```bash
   claude mcp add --transport http chorus https://<chorus-host>/mcp \
     --header "Authorization: Bearer <token>"
   ```

   **Local (Chorus runs on your principal's machine).** The whole pipeline runs
   as a subprocess of the agent over MCP stdio. No server and no token are
   needed. Set it up once from a clone:

   ```bash
   git clone https://github.com/ctmmit/chorus.git
   cd chorus
   python -m venv .venv
   .venv/Scripts/python -m pip install -r requirements.txt   # macOS/Linux: .venv/bin/python
   .venv/Scripts/python -m pip install -e . --no-deps
   ```

   That installs a `chorus-mcp` command inside the venv. Point the agent at its
   absolute path: `<clone>/.venv/Scripts/chorus-mcp.exe` on Windows,
   `<clone>/.venv/bin/chorus-mcp` on macOS and Linux. It works from any
   directory.

   Claude Code:

   ```bash
   claude mcp add --scope user --transport stdio chorus -- <clone>/.venv/Scripts/chorus-mcp.exe
   ```

   Claude Desktop (`claude_desktop_config.json`):

   ```json
   {
     "mcpServers": {
       "chorus": { "command": "<clone>/.venv/Scripts/chorus-mcp.exe" }
     }
   }
   ```

   OpenClaw accepts either form in `openclaw.json`:

   ```json5
   {
     mcp: {
       servers: {
         chorusLocal: { command: "<clone>/.venv/Scripts/chorus-mcp.exe" },
         chorus: {
           url: "https://<chorus-host>/mcp",
           transport: "streamable-http",
           headers: { Authorization: "Bearer ${CHORUS_API_TOKEN}" },
         },
       },
     },
   }
   ```

   Hermes accepts either form under `mcp_servers` in `~/.hermes/config.yaml`:

   ```yaml
   mcp_servers:
     chorus_local:
       command: "<clone>/.venv/Scripts/chorus-mcp.exe"
     chorus:
       url: "https://<chorus-host>/mcp"
       headers:
         Authorization: "Bearer ${CHORUS_API_TOKEN}"
   ```

   Local mode reads provider keys from `<clone>/.env.local` (copy it from
   `.env.local.example`). With no keys it runs deterministic mocks and spends
   nothing: digests are keyword-scored and `audio_url` is a text placeholder.
   With `ANTHROPIC_API_KEY` and `ELEVENLABS_API_KEY` set it makes real, billed
   calls. Without a transcript key it can only read the bundled catalog and
   RSS feeds that publish their own transcripts.

3. Run the `chorus-soul-bootstrap` interview. Ask the six questions, then call
   `build_soul_from_interview` and save the returned markdown as the principal's
   `soul.md`.
4. Call `list_shows`, then `submit_digest` with that soul, current context, and
   at least one episode. Poll `get_digest(job_id)` until `done` or `failed`.
   Deliver the grounded highlights, warnings, and authenticated `audio_url`.
5. To deliver this every week without being asked, subscribe. The agent flow is
   `search_podcasts` -> `preview_subscription` -> `subscribe`: find each show
   the principal follows, preview the episodes the first run would send, then
   subscribe with the soul, context, and the confirmed sources. Chorus checks
   every feed for new episodes on each run, never repeats one it already sent,
   and emails the digest (or a short "nothing new" note). Manage it with
   `list_subscriptions`, `update_subscription`, and `unsubscribe`; the
   `chorus-weekly` skill walks through each step.

The HTTP API exposes the same flow at `POST /digest` and `GET /digest/{job_id}`.
Read [`SKILL.md`](SKILL.md) for its complete request, response, and failure contract.

## What it is

Chorus turns podcast transcripts into a principal-specific weekly brief and
short audio episode. Its curation lens is explicit, every surfaced quote points
back to a real timestamp, and irrelevant episodes are refused instead of padded.

## Architecture

1. An agent connects through the Agent Skill, MCP, or the FastAPI/OpenAPI surface.
2. FastAPI enforces request limits and accepts a master token or hashed issued key.
3. `JobStore` persists the asynchronous lifecycle in SQLite.
4. The pipeline ingests transcripts, scores grounded windows, composes a script, and renders audio.
5. The agent polls the job and delivers the digest plus its protected audio artifact.

## Local development

Python 3.12 is required. Real providers are optional; tests and the golden path
use fixtures and mocks.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pip install -e . --no-deps
Copy-Item .env.local.example .env.local
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check chorus tests scripts
.venv\Scripts\python.exe -m mypy chorus
.venv\Scripts\python.exe scripts/golden_path.py
```

`requirements-dev.txt` holds the exact pinned versions CI uses. The editable
install adds two commands to the venv:

- `chorus-mcp` runs the local MCP stdio server described above.
- `chorus-api` serves the HTTP API on `127.0.0.1:8000` after loading
  `.env.local`. It refuses to start when a provider key is set without
  `CHORUS_API_TOKEN`. Set `HOST=0.0.0.0` to expose it beyond loopback.

The real show transcripts used by most tests live in the private
`ctmmit/chorus-private` repository; without them those tests skip and the
synthetic `sample_public` transcript still covers the whole pipeline. See
[`fixtures/README.md`](fixtures/README.md).

## Project documents

- [`SKILL.md`](SKILL.md): complete agent-facing API contract
- [`DEPLOY.md`](DEPLOY.md): deployment configuration and provider setup
- [`docs/DEVELOPMENT_PLAN.md`](docs/DEVELOPMENT_PLAN.md): phased product and engineering plan
