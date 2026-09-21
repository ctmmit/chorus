# Chorus

## Build a Chorus agent for your principal

You are the agent. Complete these steps in order:

1. Get a token. Ask the operator for `CHORUS_API_TOKEN`, or request your own and
   retrieve it from email:

   ```bash
   curl -i -X POST "https://<chorus-host>/keys" \
     -H "Content-Type: application/json" \
     -d '{"email":"principal@example.com"}'
   ```

2. Install the Chorus skill, or connect the MCP server:

   ```bash
   npx skills add ctmmit/chorus
   ```

   For a local stdio server, clone this repository and point the client at its
   Python environment. Claude Desktop uses this JSON; Claude Code can use the
   command immediately below it.

   ```json
   {
     "mcpServers": {
       "chorus": {
         "command": "C:/path/to/chorus/.venv/Scripts/python.exe",
         "args": ["-m", "chorus.mcp_server"]
       }
     }
   }
   ```

   ```bash
   claude mcp add --transport stdio chorus -- C:/path/to/chorus/.venv/Scripts/python.exe -m chorus.mcp_server
   claude mcp add --transport http chorus https://<chorus-host>/mcp \
     --header "Authorization: Bearer <token>"
   ```

   OpenClaw accepts either form in `openclaw.json`:

   ```json5
   {
     mcp: {
       servers: {
         chorusLocal: {
           command: "C:/path/to/chorus/.venv/Scripts/python.exe",
           args: ["-m", "chorus.mcp_server"],
         },
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
       command: "C:/path/to/chorus/.venv/Scripts/python.exe"
       args: ["-m", "chorus.mcp_server"]
     chorus:
       url: "https://<chorus-host>/mcp"
       headers:
         Authorization: "Bearer ${CHORUS_API_TOKEN}"
   ```

3. Run the `chorus-soul-bootstrap` interview. Ask the six questions, then call
   `build_soul_from_interview` and save the returned markdown as the principal's
   `soul.md`.
4. Call `list_shows`, then `submit_digest` with that soul, current context, and
   at least one episode. Poll `get_digest(job_id)` until `done` or `failed`.
   Deliver the grounded highlights, warnings, and authenticated `audio_url`.

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
.venv\Scripts\python.exe -m pip install -r requirements.in -r requirements-dev.in
Copy-Item .env.local.example .env.local
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check chorus tests scripts
.venv\Scripts\python.exe -m mypy chorus
.venv\Scripts\python.exe scripts/golden_path.py
```

Run the API with `.venv\Scripts\python.exe -m chorus.app`. Run the local MCP
stdio server with `.venv\Scripts\python.exe -m chorus.mcp_server`.

## Project documents

- [`SKILL.md`](SKILL.md): complete agent-facing API contract
- [`DEPLOY.md`](DEPLOY.md): deployment configuration and provider setup
- [`docs/DEVELOPMENT_PLAN.md`](docs/DEVELOPMENT_PLAN.md): phased product and engineering plan
