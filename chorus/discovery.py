"""Discovery documents (docs/DEVELOPMENT_PLAN.md §8 row H, "Discovery"):
publishes Chorus itself, and every registered Persona, as a standard,
public, machine-readable document another agent can fetch and act on
(IDEA_DOC.md §14's North Star: "personas as agents, agents as each other's
audience"). Also exposes the persona registry as CRUD and a force-directed
network graph for the viewer (Phase G's web app).

Two discovery documents are published per agent (the service, and each
persona-as-agent), each verified against a primary source:

**A2A Agent Card** (`GET /.well-known/agent.json`, plus the current
spec-standard alias `GET /.well-known/agent-card.json`) — verified 21 Sep
2026 against `a2aproject/A2A` tag `v1.0.1` (`specification/a2a.proto`,
fetched via `gh api repos/a2aproject/A2A/contents/specification/a2a.proto`)
and `docs/topics/agent-discovery.md` in the same repo. Two things changed
since the commonly-cited v0.x shape:

1. The well-known path moved from `/.well-known/agent.json` to
   `/.well-known/agent-card.json` (RFC 8615). Chorus serves both — the
   plan's spec'd path, plus the current standard one — so both generations
   of client resolve it.
2. `a2a.proto`'s `AgentCard` message replaced a flat `url` field with
   `supported_interfaces[]` (each an `AgentInterface` with its own url/
   protocol_binding/protocol_version), and represents `securitySchemes` as a
   proto `oneof` (e.g. `httpAuthSecurityScheme: {scheme, bearerFormat}`)
   rather than an OpenAPI-style `{"type": "http", "scheme": "bearer"}`
   discriminator. Chorus is not a full A2A RPC server (no `message/send`,
   no gRPC/JSON-RPC transport) — it only publishes the discoverability
   document — so this file uses the flatter, still-widely-deployed shape
   (`name`/`description`/`url`/`provider`/`capabilities`/`securitySchemes`/
   `security`/`defaultInputModes`/`defaultOutputModes`/`skills`) documented
   in `docs/topics/agent-discovery.md`'s own field list and implemented by
   the `a2a-python` SDK's `AgentCard` model. `AgentSkill` fields (`id`,
   `name`, `description`, `tags`, `examples`, `inputModes`, `outputModes`)
   are unchanged in v1.0.1 and used as-is.

**NANDA AgentFacts** (`GET /.well-known/agent-facts.json`) — verified 21 Sep
2026 against `projnanda/agentfacts-format`'s `agentfacts_schema.json`
(fetched via `gh api repos/projnanda/agentfacts-format/contents/
agentfacts_schema.json`), the schema `projnanda/list-39` (the "list39"
AgentFacts registry referenced in NANDA index paper arXiv:2507.14263)
implements. Required fields: `id`, `agent_name`, `label`, `description`,
`version`, `provider`, `endpoints`, `capabilities`, `skills`. Where the
schema is silent (it defines no `signature` field; arXiv:2507.14263 §4
describes cryptographically-verified AgentFacts as the target state but the
published JSON Schema has not caught up), Chorus adds `signature: null` as
its own minimal placeholder — see the field's docstring below.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from chorus import __version__, catalog
from chorus.personas import Cadence, Persona, PersonaRegistry, build_persona

router = APIRouter()

PUBLIC_URL_ENV = "CHORUS_PUBLIC_URL"
DEFAULT_PUBLIC_URL = "http://localhost:8000"
# Keep in sync with chorus.app's FastAPI(version=...) — both name the same
# pre-1.0 service; not imported directly to avoid a circular import
# (chorus.app imports this module to mount `router`).
SERVICE_VERSION = __version__
PROVIDER_NAME = "Chorus"
AUTH_SCHEME_NAME = "bearer"


def public_url(request: Request | None = None) -> str:
    """CHORUS_PUBLIC_URL wins (a deployment behind a proxy knows its own name);
    otherwise the URL the caller actually reached us on, so a discovery
    document never points at a localhost default from a real host."""
    configured = os.environ.get(PUBLIC_URL_ENV)
    if configured:
        return configured.rstrip("/")
    if request is not None:
        return str(request.base_url).rstrip("/")
    return DEFAULT_PUBLIC_URL


def get_personas(request: Request) -> PersonaRegistry:
    """FastAPI dependency resolving the PersonaRegistry `create_app` (chorus/
    app.py) attaches to `app.state.personas` — indirection instead of each
    handler reading `request.app.state` directly, so tests can override this
    dependency with `app.dependency_overrides[get_personas] = ...`."""
    registry = getattr(request.app.state, "personas", None)
    if registry is None:  # pragma: no cover - programmer error, not a user path
        raise RuntimeError("PersonaRegistry not configured on app.state.personas")
    return registry


# --- A2A Agent Card ---------------------------------------------------------


def _skills() -> list[dict[str, object]]:
    return [
        {
            "id": "digest",
            "name": "Generate a digest",
            "description": (
                "Score a soul-conditioned lens against submitted episodes and "
                "return cited highlights, a two-host script, and rendered audio."
            ),
            "tags": ["podcast", "digest", "curation"],
            "examples": ["POST /digest with {soul, context, episodes, highlight_count}"],
            "inputModes": ["application/json"],
            "outputModes": ["application/json", "audio/mpeg"],
        },
        {
            "id": "select",
            "name": "Select episodes by show or id",
            "description": (
                "Resolve a catalog selection (show names and/or video ids) into "
                "episodes, then run the same digest pipeline as /digest."
            ),
            "tags": ["podcast", "catalog", "curation"],
            "examples": ["POST /digest/select with {soul, context, shows: [...]}"],
            "inputModes": ["application/json"],
            "outputModes": ["application/json", "audio/mpeg"],
        },
        {
            "id": "soul-bootstrap",
            "name": "Register a persona soul",
            "description": (
                "Register a persona's markdown soul (lens) as a discoverable agent "
                "with its own Agent Card and AgentFacts document."
            ),
            "tags": ["persona", "identity", "soul"],
            "examples": ["POST /personas with {name, soul, shows, cadence}"],
            "inputModes": ["application/json"],
            "outputModes": ["application/json"],
        },
    ]


def _security_block() -> tuple[dict[str, object], list[dict[str, list[str]]]]:
    schemes: dict[str, object] = {
        AUTH_SCHEME_NAME: {"type": "http", "scheme": "bearer"},
    }
    security: list[dict[str, list[str]]] = [{AUTH_SCHEME_NAME: []}]
    return schemes, security


@router.get("/.well-known/agent.json")
@router.get("/.well-known/agent-card.json")
def service_agent_card(request: Request) -> dict[str, object]:
    """The Chorus service itself as an A2A agent. See module docstring for
    the schema source and the v1.0.1-vs-flat-shape note."""
    schemes, security = _security_block()
    url = public_url(request)
    return {
        "name": PROVIDER_NAME,
        "description": (
            "Agent-native podcast curation and voice: submit a soul (lens) and "
            "episodes, get back cited highlights, a two-host script, and audio."
        ),
        "url": url,
        "version": SERVICE_VERSION,
        "provider": {"organization": PROVIDER_NAME, "url": url},
        "capabilities": {"streaming": False, "pushNotifications": False},
        "securitySchemes": schemes,
        "security": security,
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json", "audio/mpeg"],
        "skills": _skills(),
    }


def _persona_publication_line(persona: Persona) -> str:
    """"the persona as an agent that 'publishes a weekly episode on
    <shows>'" (spec wording, generalized to the persona's own cadence)."""
    shows_str = ", ".join(persona.shows) if persona.shows else "no shows yet"
    line = f"{persona.name} publishes a {persona.cadence} episode on {shows_str}."
    return f"{persona.description} {line}" if persona.description else line


def _persona_agent_card(persona: Persona, base: str) -> dict[str, object]:
    schemes, security = _security_block()
    url = f"{base}/personas/{persona.persona_id}"
    description = _persona_publication_line(persona)
    return {
        "name": persona.name,
        "description": description,
        "url": url,
        "version": persona.soul_version,
        "provider": {"organization": PROVIDER_NAME, "url": base},
        "capabilities": {"streaming": False, "pushNotifications": False},
        "securitySchemes": schemes,
        "security": security,
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json", "audio/mpeg"],
        "skills": [
            {
                "id": "episode-feed",
                "name": f"{persona.name} episode feed",
                "description": description,
                "tags": ["podcast", "persona", *persona.shows],
                "examples": [f"GET {url} for this persona's registered facts"],
                "inputModes": ["application/json"],
                "outputModes": ["application/json", "audio/mpeg"],
            }
        ],
    }


# --- NANDA AgentFacts --------------------------------------------------------


def _service_agent_facts(base: str) -> dict[str, object]:
    url = base
    return {
        "id": "chorus:service",
        "agent_name": "urn:agent:chorus:service",
        "label": PROVIDER_NAME,
        "description": (
            "Agent-native podcast curation and voice service. See "
            "/.well-known/agent.json for the A2A view of the same agent."
        ),
        "version": SERVICE_VERSION,
        "documentationUrl": f"{url}/docs",
        "provider": {"name": PROVIDER_NAME, "url": url},
        "endpoints": {"static": [f"{url}/digest", f"{url}/digest/select"]},
        "capabilities": {
            "modalities": ["text", "audio"],
            "streaming": False,
            "batch": False,
            "authentication": {"methods": [AUTH_SCHEME_NAME]},
        },
        "skills": [
            {
                "id": s["id"],
                "description": s["description"],
                "inputModes": s["inputModes"],
                "outputModes": s["outputModes"],
            }
            for s in _skills()
        ],
        # NANDA index paper (arXiv:2507.14263) describes cryptographically
        # signed AgentFacts as the trust mechanism; the published
        # agentfacts_schema.json has no `signature` field yet, so this is
        # Chorus's own placeholder. Ed25519 signing is a follow-up (H is
        # discovery-document plumbing, not the trust layer).
        "signature": None,
    }


def _persona_agent_facts(persona: Persona, base: str) -> dict[str, object]:
    url = f"{base}/personas/{persona.persona_id}"
    return {
        "id": f"chorus:persona:{persona.persona_id}",
        "agent_name": f"urn:agent:chorus:persona:{persona.persona_id}",
        "label": persona.name,
        "description": _persona_publication_line(persona),
        "version": persona.soul_version,
        "provider": {"name": PROVIDER_NAME, "url": base},
        "endpoints": {"static": [url]},
        "capabilities": {
            "modalities": ["text", "audio"],
            "streaming": False,
            "batch": False,
            "authentication": {"methods": [AUTH_SCHEME_NAME]},
        },
        "skills": [
            {
                "id": "episode-feed",
                "description": _persona_publication_line(persona),
                "inputModes": ["application/json"],
                "outputModes": ["application/json", "audio/mpeg"],
            }
        ],
        "signature": None,  # see _service_agent_facts's docstring comment
    }


@router.get("/.well-known/agent-facts.json")
def service_agent_facts(request: Request) -> dict[str, object]:
    return _service_agent_facts(public_url(request))


# --- Persona registry CRUD ---------------------------------------------------


class CreatePersonaRequest(BaseModel):
    name: str
    description: str = ""
    soul: str
    shows: list[str] = []
    cadence: Cadence = "weekly"
    public: bool = True


@router.get("/personas")
def list_personas(personas: PersonaRegistry = Depends(get_personas)) -> list[Persona]:
    """Public listing — public personas only, regardless of caller."""
    return personas.list(public_only=True)


@router.post("/personas")
def create_persona(
    request: CreatePersonaRequest, personas: PersonaRegistry = Depends(get_personas)
) -> Persona:
    """Bearer-authed (see chorus.app's DISCOVERY_PUBLIC_PREFIXES: only GET
    under /personas is auth-exempt)."""
    persona = build_persona(
        name=request.name,
        description=request.description,
        soul=request.soul,
        shows=request.shows,
        cadence=request.cadence,
        public=request.public,
    )
    return personas.create(persona)


@router.get("/personas/{persona_id}")
def get_persona(persona_id: str, personas: PersonaRegistry = Depends(get_personas)) -> Persona:
    persona = personas.get(persona_id)
    if persona is None:
        raise HTTPException(status_code=404, detail="unknown persona_id")
    return persona


@router.delete("/personas/{persona_id}")
def delete_persona(persona_id: str, personas: PersonaRegistry = Depends(get_personas)) -> dict[str, bool]:
    deleted = personas.delete(persona_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="unknown persona_id")
    return {"deleted": True}


@router.get("/personas/{persona_id}/agent.json")
def persona_agent_card(
    persona_id: str, request: Request, personas: PersonaRegistry = Depends(get_personas)
) -> dict[str, object]:
    persona = personas.get(persona_id)
    if persona is None:
        raise HTTPException(status_code=404, detail="unknown persona_id")
    return _persona_agent_card(persona, public_url(request))


@router.get("/personas/{persona_id}/agent-facts.json")
def persona_agent_facts(
    persona_id: str, request: Request, personas: PersonaRegistry = Depends(get_personas)
) -> dict[str, object]:
    persona = personas.get(persona_id)
    if persona is None:
        raise HTTPException(status_code=404, detail="unknown persona_id")
    return _persona_agent_facts(persona, public_url(request))


# --- Network graph -----------------------------------------------------------


class NetworkNode(BaseModel):
    id: str
    kind: str  # "persona" | "show"
    label: str
    size: int


class NetworkEdge(BaseModel):
    source: str
    target: str
    kind: str  # "listens_to"


class NetworkGraph(BaseModel):
    nodes: list[NetworkNode]
    edges: list[NetworkEdge]


def _show_node_id(show_key: str) -> str:
    return f"show:{show_key}"


def build_network(personas: list[Persona]) -> NetworkGraph:
    """Nodes: one per public persona (size = shows listened to) plus one per
    catalog show (size = number of listening personas, including 0 for a
    show nobody has subscribed to yet — the graph should still show the
    whole show universe, not just the subscribed slice). Show names are
    matched against the catalog case-insensitively (chorus.catalog.resolve's
    own convention); a persona listing a show absent from the catalog still
    gets a node for it, keyed by its own spelling, so a registration typo
    doesn't silently vanish from the graph."""
    catalog_shows = [s["show"] for s in catalog.list_shows()]
    canonical_by_key = {name.lower(): name for name in catalog_shows}

    show_listeners: dict[str, int] = dict.fromkeys(canonical_by_key, 0)
    show_label: dict[str, str] = dict(canonical_by_key)

    persona_nodes: list[NetworkNode] = []
    edges: list[NetworkEdge] = []
    for persona in personas:
        persona_nodes.append(
            NetworkNode(
                id=persona.persona_id,
                kind="persona",
                label=persona.name,
                size=len(persona.shows),
            )
        )
        seen: set[str] = set()
        for raw_show in persona.shows:
            key = raw_show.lower()
            if key in seen:
                continue
            seen.add(key)
            show_label.setdefault(key, raw_show)
            show_listeners[key] = show_listeners.get(key, 0) + 1
            edges.append(
                NetworkEdge(source=persona.persona_id, target=_show_node_id(key), kind="listens_to")
            )

    show_nodes = [
        NetworkNode(id=_show_node_id(key), kind="show", label=label, size=show_listeners.get(key, 0))
        for key, label in show_label.items()
    ]
    return NetworkGraph(nodes=[*persona_nodes, *show_nodes], edges=edges)


@router.get("/network")
def network(personas: PersonaRegistry = Depends(get_personas)) -> NetworkGraph:
    return build_network(personas.list(public_only=True))
