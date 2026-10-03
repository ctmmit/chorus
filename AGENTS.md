# AGENTS.md

## Setting Chorus up for a principal

If a principal handed you this repository to set Chorus up for them (not to
develop it), stop here and follow [`INSTALL_FOR_AGENTS.md`](INSTALL_FOR_AGENTS.md).
The rest of this file is for contributors changing the code.

## Repository map

- `chorus/app.py`: FastAPI routes, auth, request limits, and mounted MCP transport.
- `chorus/models.py`: Pydantic v2 request, response, and pipeline models.
- `chorus/mcp_server.py`: FastMCP tools plus stdio and Streamable HTTP entrypoints.
- `chorus/keys.py`, `chorus/email.py`: self-serve API-key and delivery seams.
- `chorus/jobs.py`, `chorus/stores/postgres.py`: SQLite and Postgres persistence behind one Protocol.
- `chorus/pipeline.py`: ingest → curate → script → audio orchestration.
- `chorus/bootstrap.py`: source-agnostic soul builders.
- `chorus/onboarding.py`, `chorus/soul.py`: onboarding steps and soul validation (`~/.chorus/config.toml`).
- `chorus/agent_setup.py`: transport-neutral onboarding for host agents; `chorus/mcp_setup.py` (local MCP) and `chorus setup` (JSON CLI) expose it.
- `chorus/wizard.py`, `chorus/cli.py`: the `chorus onboard` terminal wizard and `chorus` command.
- `chorus/paths.py`: every local state location under `~/.chorus/`.
- `fixtures/`: public test inputs; real transcripts are gitignored.
- `tests/`: offline unit and integration tests.
- `scripts/golden_path.py`: deterministic end-to-end acceptance path.

## Commands

Set `$PY` to the repository's Python 3.12 interpreter. Install once with
`& $PY -m pip install -r requirements-dev.txt` then `& $PY -m pip install -e . --no-deps`,
then run from the root:

```powershell
& $PY -m pytest -q
& $PY -m ruff check chorus tests scripts
& $PY -m mypy chorus
& $PY scripts/golden_path.py
```

All four commands must pass before a commit. Never call live providers in tests.

## Conventions

- Use type hints on every function signature and Pydantic models at boundaries.
- Use `Protocol` plus mock and real implementations for external services.
- Use `httpx`, `pathlib`, named constants, and explicit error states.
- Keep financial or scoring calculations pure and independently testable.
- Ruff line length is 100; mypy must remain clean.
- Do not commit `.env.local`, provider credentials, databases, audio artifacts, or real transcripts.
- Branch names use `feature/`, `fix/`, or `refactor/`; commits are imperative and under 72 characters.

## Working alongside other agents

Several agent sessions often work in this repository at once. These rules keep
them from breaking each other's work.

- Work in your own git worktree (`.claude/worktrees/<name>`), never in the main
  checkout. The main checkout stays on `main` because dev servers run from it;
  switching its branch changes files under them.
- Push your feature branch after every commit (`git push -u origin <branch>` the
  first time). An unpushed commit exists only on one laptop.
- `main` is protected: changes land through a pull request with the `python` and
  `web` CI checks green. Do not push to `main` directly.
- Merge in small pieces. When one part of a feature passes the four commands
  above, open a pull request for it instead of stacking more commits on the
  branch. Rebase onto `origin/main` before opening it.
- CI skips tests that need the private transcripts. Before merging anything that
  touches transcripts, curation, or scripts, run the four commands locally with
  the private fixtures present (`scripts/sync_private_fixtures`) and say so in
  the pull request.
- Register new MCP tools in your feature's own `register_*_tools(server, ...)`
  function and call it from `create_mcp_server`, rather than adding lines to the
  shared tool list. Shared lists are where parallel branches conflict.
