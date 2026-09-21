"""Federation Forge roundtrip — ``arango`` dialect (ADR-0006 D-3, S2).

``introspect(generate(O)) ≡ O`` through the REAL Arango-side analyzer:

    forge.generate(O, "arango") -> throwaway database, loaded by the GENERATED
                                   loader script (forge.load.py, python-arango)
                                -> arangodb-schema-analyzer AgenticSchemaAnalyzer
                                   (deterministic baseline, no LLM)
                                -> compare its conceptual schema to O

ASA's baseline reports entities as ``pascal_case(singularize(collection))``,
properties by their raw document field names (``account_name``), and dedicated
edge collections as ``COLLECTION_NAME`` relationships whose endpoints come
from the sampled ``_from``/``_to`` prefixes — so the comparison normalizes
property and relationship spellings through the same CC-12 normalizers the
other dialects use (``forge_support``). The baseline does not type properties
from samples (everything is ``string``), so type fidelity is asserted on the
SQL dialects only; here the ``rows`` are checked to have landed verbatim.

Requires the compose stack's ArangoDB and ``arangodb-schema-analyzer``
(``pip install -e ".[dev]"``); skipped cleanly otherwise.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, Dict, Tuple

import pytest

from r2g.forge import ForgeArtifacts, ForgeOntology, generate, table_name
from r2g.forge.dialects.arango import GRAPH_NAME, project_documents

from .conftest import ARANGO_ENDPOINT, ARANGO_PASSWORD, ARANGO_USER, requires_arango
from .forge_support import (
    CONCEPTUAL,
    ROWS_PER_ENTITY,
    SEED,
    assert_conceptual_model_matches,
    forge_ontology,
)

pytestmark = requires_arango

schema_analyzer = pytest.importorskip("schema_analyzer", reason="arangodb-schema-analyzer not installed")


@pytest.fixture
def loaded_federation(arango_test_db, tmp_path) -> Tuple[ForgeOntology, ForgeArtifacts, Any]:
    """generate(O) written to disk and loaded by running the generated loader
    script exactly as a user would; yields ``(ontology, artifacts, db)``."""
    db_name, db = arango_test_db
    ontology = forge_ontology()
    artifacts = generate(ontology, dialect="arango", seed=SEED, rows_per_entity=ROWS_PER_ENTITY)
    paths = artifacts.write_to(str(tmp_path))
    loader = next(p for p in paths if p.endswith("forge.load.py"))

    env = {
        **os.environ,
        "ARANGO_ENDPOINT": ARANGO_ENDPOINT,
        "ARANGO_DB": db_name,
        "ARANGO_USER": ARANGO_USER,
        "ARANGO_PASSWORD": ARANGO_PASSWORD,
    }
    result = subprocess.run([sys.executable, loader], env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, f"loader failed:\n{result.stdout}\n{result.stderr}"
    return ontology, artifacts, db


def _conceptual_dict(result: Any) -> Dict[str, Any]:
    conceptual = result.conceptual_schema
    if isinstance(conceptual, dict):
        return conceptual
    if hasattr(conceptual, "to_json"):
        return conceptual.to_json()
    return conceptual.model_dump()


def _analyze(db: Any, entity_strategy: str) -> Any:
    from schema_analyzer.analyzer import AgenticSchemaAnalyzer

    analyzer = AgenticSchemaAnalyzer(llm_provider=None)  # deterministic baseline, no network
    return analyzer.analyze_physical_schema(db, entity_strategy=entity_strategy, use_cache=False)


def test_loader_script_created_the_planned_collections(loaded_federation):
    ontology, artifacts, db = loaded_federation
    manifest = json.loads(artifacts.ddl)
    for collection in manifest["collections"]:
        assert db.has_collection(collection["name"])
        assert db.collection(collection["name"]).count() == ROWS_PER_ENTITY
    for edge in manifest["edgeCollections"]:
        col = db.collection(edge["name"])
        assert col.properties()["edge"] is True
        assert col.count() == ROWS_PER_ENTITY
    assert db.has_graph(GRAPH_NAME)
    assert {e["edge_collection"] for e in db.graph(GRAPH_NAME).edge_definitions()} == {
        e["name"] for e in manifest["edgeCollections"]
    }


def test_documents_landed_verbatim_and_edges_hit_the_spine(loaded_federation):
    """The pre-partition rows are in the documents unchanged (F-5) and every
    edge resolves to an existing vertex on both ends — no dangling ``_to``."""
    ontology, artifacts, db = loaded_federation
    expected = project_documents(json.loads(artifacts.ddl), artifacts.rows)
    for name, documents in expected.items():
        stored = {d["_key"]: d for d in db.collection(name).all()}
        assert set(stored) == {d["_key"] for d in documents}
        for doc in documents:
            got = {k: v for k, v in stored[doc["_key"]].items() if k not in ("_id", "_rev")}
            assert got == doc, f"{name}/{doc['_key']} differs"
    for r in ontology.relationships:
        edge_collection = f"{table_name(r.from_entity)}_to_{table_name(r.to_entity)}"
        dangling = db.aql.execute(
            f"FOR e IN {edge_collection} FILTER DOCUMENT(e._to) == null OR DOCUMENT(e._from) == null "
            "COLLECT WITH COUNT INTO n RETURN n"
        )
        assert next(dangling) == 0


@pytest.mark.parametrize("entity_strategy", ["auto", "collection"])
def test_asa_baseline_reproduces_the_ontology(loaded_federation, entity_strategy, record_property):
    """The D-3 property through the REAL ASA baseline, under both the default
    (auto-detect LPG vs PG) and the explicit collection-per-entity strategy —
    the generated shape is PG style, and ``auto`` must recognize it as such."""
    ontology, _artifacts, db = loaded_federation
    result = _analyze(db, entity_strategy)
    conceptual = _conceptual_dict(result)
    assert_conceptual_model_matches(ontology, conceptual["entities"], conceptual["relationships"])
    for relationship in conceptual["relationships"]:
        assert relationship.get("properties", []) == [], "edges carry no relationship properties"

    metadata = result.metadata.model_dump() if hasattr(result.metadata, "model_dump") else dict(result.metadata)
    patterns = metadata.get("detected_patterns") or metadata.get("detectedPatterns") or []
    assert "PG_ENTITY_COLLECTION" in patterns and "PG_DEDICATED_EDGE" in patterns, patterns
    assert not any(p.startswith("LPG_") for p in patterns), f"forge emits PG style; got {patterns}"
    record_property(f"forge_arango_patterns_{entity_strategy}", sorted(patterns))

#: A second ontology sharing no entity or relationship with the first. The
#: graph name is a fixed constant and ARANGO_DB defaults to _system, so two
#: ontologies loaded on one machine land on the same graph — which is the whole
#: hazard the idempotence fix addresses.
_SECOND_CONCEPTUAL = {
    "entities": [
        {"name": "Vendor", "properties": [{"name": "vendorName", "type": "string"}]},
        {"name": "Invoice", "properties": [{"name": "amountDue", "type": "float"}]},
    ],
    "relationships": [
        {"type": "invoicesToVendors", "fromEntity": "Invoice", "toEntity": "Vendor"},
    ],
}


def _run_loader(artifacts: ForgeArtifacts, target_dir, db_name: str) -> None:
    """Write the artifacts and run the generated loader exactly as a user would."""
    target_dir.mkdir(parents=True, exist_ok=True)
    paths = artifacts.write_to(str(target_dir))
    loader = next(p for p in paths if p.endswith("forge.load.py"))
    env = {
        **os.environ,
        "ARANGO_ENDPOINT": ARANGO_ENDPOINT,
        "ARANGO_DB": db_name,
        "ARANGO_USER": ARANGO_USER,
        "ARANGO_PASSWORD": ARANGO_PASSWORD,
    }
    result = subprocess.run(
        [sys.executable, loader], env=env, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, f"loader failed:\n{result.stdout}\n{result.stderr}"


def _edge_collections_on_the_graph(db, name: str = GRAPH_NAME) -> set:
    return {d["edge_collection"] for d in db.graph(name).edge_definitions()}


@requires_arango
def test_a_second_ontology_does_not_inherit_the_first_graph(arango_test_db, tmp_path):
    """Loading a different ontology into the same database must leave the graph
    describing the *second* one.

    Regression, and the reason this is a live test rather than a string
    assertion: the loader skipped ``create_graph`` whenever a graph of that name
    already existed. The second ontology's collections imported fine, the graph
    kept the FIRST ontology's edge definitions, and the script exited 0 — so the
    analyzer read relationships that no longer existed anywhere in the data. A
    test that greps the generated script for ``delete_graph`` pins the shape of
    the fix; only running two ontologies pins the behaviour it was for.
    """
    db_name, db = arango_test_db

    first = ForgeOntology.from_conceptual(dict(CONCEPTUAL))
    _run_loader(
        generate(first, dialect="arango", seed=SEED, rows_per_entity=ROWS_PER_ENTITY),
        tmp_path / "first",
        db_name,
    )
    first_edges = _edge_collections_on_the_graph(db)
    assert first_edges == {"contacts_to_accounts", "support_tickets_to_contacts"}

    second = ForgeOntology.from_conceptual(dict(_SECOND_CONCEPTUAL))
    _run_loader(
        generate(second, dialect="arango", seed=SEED, rows_per_entity=ROWS_PER_ENTITY),
        tmp_path / "second",
        db_name,
    )
    second_edges = _edge_collections_on_the_graph(db)

    assert second_edges == {"invoices_to_vendors"}, (
        f"graph still describes the previous ontology: {sorted(second_edges)}"
    )
    assert not (second_edges & first_edges), "a stale edge definition survived the reload"


@requires_arango
def test_reloading_the_same_ontology_is_idempotent(arango_test_db, tmp_path):
    """The other half of the claim: a re-run of the *same* ontology must leave
    the graph and the document counts exactly as they were."""
    db_name, db = arango_test_db
    ontology = forge_ontology()
    artifacts = generate(ontology, dialect="arango", seed=SEED, rows_per_entity=ROWS_PER_ENTITY)

    _run_loader(artifacts, tmp_path / "once", db_name)
    edges_once = _edge_collections_on_the_graph(db)
    counts_once = {t: db.collection(t).count() for t in edges_once | {"accounts", "contacts"}}

    _run_loader(artifacts, tmp_path / "twice", db_name)
    assert _edge_collections_on_the_graph(db) == edges_once
    assert {t: db.collection(t).count() for t in counts_once} == counts_once, (
        "a second load of the same ontology changed the data"
    )
