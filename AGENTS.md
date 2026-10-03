# AGENTS.md

## Repository map

- `chorus/app.py`: FastAPI routes, auth, request limits, and mounted MCP transport.
- `chorus/models.py`: Pydantic v2 request, response, and pipeline models.
- `chorus/mcp_server.py`: FastMCP tools plus stdio and Streamable HTTP entrypoints.
- `chorus/keys.py`, `chorus/email.py`: self-serve API-key and delivery seams.
- `chorus/jobs.py`, `chorus/stores/postgres.py`: SQLite and Postgres persistence behind one Protocol.
- `chorus/pipeline.py`: ingest → curate → script → audio orchestration.
- `chorus/bootstrap.py`: source-agnostic soul builders.
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
