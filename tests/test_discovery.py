"""Goal 8.H — discovery documents, the persona registry, and the network
graph (docs/DEVELOPMENT_PLAN.md §8 row H). Offline: SQLite personas, fixture
catalog, no network calls. Schema assertions check the shapes documented in
chorus/discovery.py's module docstring (A2A Agent Card v1.0.1's flat-shape
subset; NANDA agentfacts_schema.json's required fields), not full JSON Schema
validation against those upstream files.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chorus.app import create_app
from chorus.artifacts import LocalArtifactStore
from chorus.audio import MockAudioRenderer
from chorus.jobs import SqliteJobStore
from chorus.llm import MockLLMClient
from chorus.personas import SqlitePersonaRegistry, build_persona
from chorus.pipeline import Deps
from chorus.script import MockScriptComposer
from chorus.transcripts import FixtureTranscriptProvider

FIX = Path(__file__).resolve().parent.parent / "fixtures"
A2A_REQUIRED_SKILL_IDS = {"digest", "select", "soul-bootstrap"}
AGENTFACTS_REQUIRED_FIELDS = {
    "id",
    "agent_name",
    "label",
    "description",
    "version",
    "provider",
    "endpoints",
    "capabilities",
    "skills",
}


def _deps(tmp_path: Path) -> Deps:
    return Deps(
        provider=FixtureTranscriptProvider(),
        llm=MockLLMClient(),
        composer=MockScriptComposer(),
        renderer=MockAudioRenderer(out_dir=tmp_path / "artifacts"),
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
    )


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(SqliteJobStore(tmp_path / "jobs.db"), _deps(tmp_path)))


@pytest.fixture
def secured(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(SqliteJobStore(tmp_path / "jobs.db"), _deps(tmp_path), api_token="secret")
    )


@pytest.fixture
def registry(tmp_path: Path) -> SqlitePersonaRegistry:
    return SqlitePersonaRegistry(tmp_path / "personas.db")


# --- Persona registry CRUD (chorus/personas.py, no HTTP) --------------------


def test_registry_create_get_list_delete(registry: SqlitePersonaRegistry) -> None:
    p = build_persona(name="Value Investor", soul="Be skeptical of hype.", shows=["Acquired"])
    registry.create(p)

    fetched = registry.get(p.persona_id)
    assert fetched is not None
    assert fetched.name == "Value Investor"
    assert fetched.soul_version  # sha1 prefix, non-empty

    assert [x.persona_id for x in registry.list()] == [p.persona_id]
    assert registry.delete(p.persona_id) is True
    assert registry.get(p.persona_id) is None
    assert registry.delete(p.persona_id) is False


def test_registry_list_public_only(registry: SqlitePersonaRegistry) -> None:
    pub = build_persona(name="Public One", soul="soul a", public=True)
    priv = build_persona(name="Private One", soul="soul b", public=False)
    registry.create(pub)
    registry.create(priv)

    all_personas = registry.list()
    public_only = registry.list(public_only=True)
    assert {p.persona_id for p in all_personas} == {pub.persona_id, priv.persona_id}
    assert {p.persona_id for p in public_only} == {pub.persona_id}


def test_build_persona_computes_soul_version_deterministically() -> None:
    from chorus.curation import soul_version

    p = build_persona(name="X", soul="the same soul text")
    assert p.soul_version == soul_version("the same soul text")


# --- A2A Agent Card ----------------------------------------------------------


def test_service_agent_card_shape(client: TestClient) -> None:
    body = client.get("/.well-known/agent.json").json()
    assert body["name"]
    assert body["description"]
    assert body["url"]
    assert body["version"]
    assert body["provider"]["organization"]
    assert "capabilities" in body
    assert body["securitySchemes"]["bearer"] == {"type": "http", "scheme": "bearer"}
    assert body["security"] == [{"bearer": []}]
    skill_ids = {s["id"] for s in body["skills"]}
    assert A2A_REQUIRED_SKILL_IDS <= skill_ids
    for skill in body["skills"]:
        assert skill["id"] and skill["name"] and skill["description"]
        assert isinstance(skill["tags"], list)
        assert isinstance(skill["examples"], list)
        assert isinstance(skill["inputModes"], list)
        assert isinstance(skill["outputModes"], list)


def test_agent_card_alias_matches_well_known_path(client: TestClient) -> None:
    """Current A2A spec (v1.0.1) standardized on /.well-known/agent-card.json;
    Chorus serves both that and the plan's spec'd /.well-known/agent.json."""
    a = client.get("/.well-known/agent.json").json()
    b = client.get("/.well-known/agent-card.json").json()
    assert a == b


def test_agent_card_url_honors_public_url_env(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    monkeypatch.setenv("CHORUS_PUBLIC_URL", "https://chorus.example.com")
    body = client.get("/.well-known/agent.json").json()
    assert body["url"] == "https://chorus.example.com"


# --- NANDA AgentFacts ---------------------------------------------------------


def test_service_agent_facts_shape(client: TestClient) -> None:
    body = client.get("/.well-known/agent-facts.json").json()
    assert AGENTFACTS_REQUIRED_FIELDS <= set(body.keys())
    assert body["provider"]["name"]
    assert body["provider"]["url"]
    assert body["endpoints"]["static"]
    assert "modalities" in body["capabilities"]
    assert "authentication" in body["capabilities"]
    assert "bearer" in body["capabilities"]["authentication"]["methods"]
    assert len(body["skills"]) >= 1
    for skill in body["skills"]:
        assert {"id", "description", "inputModes", "outputModes"} <= set(skill.keys())
    # Ed25519 signing is a follow-up (module docstring) — placeholder for now.
    assert body["signature"] is None


# --- Personas over HTTP -------------------------------------------------------


def _persona_payload(**overrides: object) -> dict:
    payload = {
        "name": "Value Investor",
        "description": "Skeptical of hype, allergic to adjectives.",
        "soul": (FIX / "souls" / "soul_investor.md").read_text(encoding="utf-8"),
        "shows": ["Acquired"],
        "cadence": "weekly",
        "public": True,
    }
    payload.update(overrides)
    return payload


def test_get_personas_is_public_without_token(secured: TestClient) -> None:
    r = secured.get("/personas")
    assert r.status_code == 200
    assert r.json() == []


def test_post_personas_requires_auth(secured: TestClient) -> None:
    r = secured.post("/personas", json=_persona_payload())
    assert r.status_code == 401


def test_create_and_fetch_persona(secured: TestClient) -> None:
    headers = {"Authorization": "Bearer secret"}
    created = secured.post("/personas", json=_persona_payload(), headers=headers).json()
    assert created["persona_id"]
    assert created["public"] is True

    # Listing and single-fetch are public (no Authorization header sent).
    listed = secured.get("/personas").json()
    assert [p["persona_id"] for p in listed] == [created["persona_id"]]

    fetched = secured.get(f"/personas/{created['persona_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "Value Investor"


def test_private_persona_excluded_from_listing_but_directly_fetchable(secured: TestClient) -> None:
    headers = {"Authorization": "Bearer secret"}
    created = secured.post(
        "/personas", json=_persona_payload(public=False), headers=headers
    ).json()

    assert secured.get("/personas").json() == []
    fetched = secured.get(f"/personas/{created['persona_id']}")
    assert fetched.status_code == 200


def test_delete_persona_requires_auth_then_succeeds(secured: TestClient) -> None:
    headers = {"Authorization": "Bearer secret"}
    created = secured.post("/personas", json=_persona_payload(), headers=headers).json()
    persona_id = created["persona_id"]

    assert secured.delete(f"/personas/{persona_id}").status_code == 401
    assert secured.delete(f"/personas/{persona_id}", headers=headers).status_code == 200
    assert secured.get(f"/personas/{persona_id}").status_code == 404


def test_get_unknown_persona_404s(client: TestClient) -> None:
    assert client.get("/personas/does-not-exist").status_code == 404
    assert client.get("/personas/does-not-exist/agent.json").status_code == 404
    assert client.get("/personas/does-not-exist/agent-facts.json").status_code == 404


def test_per_persona_agent_card_and_facts(client: TestClient) -> None:
    created = client.post("/personas", json=_persona_payload()).json()
    persona_id = created["persona_id"]

    card = client.get(f"/personas/{persona_id}/agent.json").json()
    assert card["name"] == "Value Investor"
    assert card["url"].endswith(f"/personas/{persona_id}")
    assert "Acquired" in card["description"]
    assert card["skills"][0]["id"] == "episode-feed"

    facts = client.get(f"/personas/{persona_id}/agent-facts.json").json()
    assert AGENTFACTS_REQUIRED_FIELDS <= set(facts.keys())
    assert facts["label"] == "Value Investor"
    assert facts["signature"] is None


# --- Network graph -------------------------------------------------------------


def test_network_shape_and_sizing(client: TestClient) -> None:
    client.post("/personas", json=_persona_payload(name="A", shows=["Acquired"]))
    client.post("/personas", json=_persona_payload(name="B", shows=["Acquired", "All-In"]))
    # A show name not present in the fixture catalog should still get a node.
    client.post("/personas", json=_persona_payload(name="C", shows=["Some Unlisted Show"]))

    body = client.get("/network").json()
    nodes_by_id = {n["id"]: n for n in body["nodes"]}
    persona_nodes = [n for n in body["nodes"] if n["kind"] == "persona"]
    show_nodes = [n for n in body["nodes"] if n["kind"] == "show"]

    assert len(persona_nodes) == 3
    a_node = next(n for n in persona_nodes if n["label"] == "A")
    b_node = next(n for n in persona_nodes if n["label"] == "B")
    assert a_node["size"] == 1  # one show
    assert b_node["size"] == 2  # two shows

    acquired_node = next(n for n in show_nodes if n["label"].lower() == "acquired")
    assert acquired_node["size"] == 2  # A and B both listen
    unlisted_node = next(n for n in show_nodes if n["label"] == "Some Unlisted Show")
    assert unlisted_node["size"] == 1

    # Every edge references node ids that actually exist.
    for edge in body["edges"]:
        assert edge["source"] in nodes_by_id
        assert edge["target"] in nodes_by_id
        assert edge["kind"] == "listens_to"


def test_network_excludes_private_personas(client: TestClient) -> None:
    client.post("/personas", json=_persona_payload(name="Hidden", shows=["Acquired"], public=False))
    body = client.get("/network").json()
    persona_labels = {n["label"] for n in body["nodes"] if n["kind"] == "persona"}
    assert "Hidden" not in persona_labels


def test_network_is_public_without_token(secured: TestClient) -> None:
    assert secured.get("/network").status_code == 200


# --- Well-known exemption under a configured token ----------------------------


def test_well_known_exempt_when_token_configured(secured: TestClient) -> None:
    assert secured.get("/.well-known/agent.json").status_code == 200
    assert secured.get("/.well-known/agent-card.json").status_code == 200
    assert secured.get("/.well-known/agent-facts.json").status_code == 200


def test_bearer_still_required_on_non_discovery_routes(secured: TestClient) -> None:
    assert secured.get("/shows").status_code == 401
