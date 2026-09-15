"""The Federation Forge roundtrip — ADR-0006 D-3, the S1 walking skeleton gate.

``introspect(generate(O)) == O`` through the REAL forward pipeline, never
through forge-internal shortcuts:

    forge.generate(O)  ->  live Postgres (fresh database, DDL + rows loaded)
                       ->  PostgresConnector.get_schema()            (real introspection)
                       ->  ConfigManager.generate_default_config()   (real Auto-Map)
                       ->  mapping_to_csi()                          (real CSI emitter)
                       ->  compare conceptual model to O

Equality is honest, not naive (PLAN F-2/F-3): names compare through the CC-12
normalizers, types through ``pg_type_to_json_type``, and the generated
surrogate plumbing (``id`` PKs, FK columns) is the *only* tolerated extra —
anything else unexpected fails. The RSA leg runs the same schema through
``rsa_ontology`` so both real analyzers are in the loop.
"""

from __future__ import annotations

import uuid

import pytest

from r2g.config import ConfigManager, pg_type_to_json_type
from r2g.csi import mapping_to_csi, validate_csi
from r2g.forge import (
    ForgeOntology,
    column_name,
    foreign_key_column,
    generate,
    table_name,
)
from r2g.rsa_ontology import propose_ontology_from_schema

from .conftest import PG_CONN, requires_pg

pytestmark = requires_pg

SEED = 421
ROWS_PER_ENTITY = 20

CONCEPTUAL = {
    "entities": [
        {
            "name": "Account",
            "properties": [
                {"name": "accountName", "type": "string"},
                {"name": "healthScore", "type": "float"},
                {"name": "seatsSold", "type": "integer"},
            ],
        },
        {
            "name": "Contact",
            "properties": [
                {"name": "fullName", "type": "string"},
                {"name": "isPrimary", "type": "boolean"},
            ],
        },
        {
            "name": "SupportTicket",
            "properties": [
                {"name": "severity", "type": "integer"},
                {"name": "resolved", "type": "boolean"},
            ],
        },
    ],
    "relationships": [
        {"type": "contactsToAccounts", "fromEntity": "Contact", "toEntity": "Account"},
        {"type": "supportTicketsToContacts", "fromEntity": "SupportTicket", "toEntity": "Contact"},
    ],
}


def _pg_dsn(db: str) -> str:
    base, _, _ = PG_CONN.rpartition("/")
    return f"{base}/{db}"


@pytest.fixture
def forge_db():
    """A fresh throwaway database the generated federation is loaded into."""
    import psycopg

    name = f"forge_rt_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(PG_CONN, autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {name}")
    try:
        yield _pg_dsn(name)
    finally:
        with psycopg.connect(PG_CONN, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE {name} WITH (FORCE)")


@pytest.fixture
def loaded_federation(forge_db):
    """generate(O) executed against the fresh database; yields (ontology,
    artifacts, dsn)."""
    import psycopg

    ontology = ForgeOntology.from_conceptual(CONCEPTUAL)
    artifacts = generate(ontology, dialect="postgres", seed=SEED, rows_per_entity=ROWS_PER_ENTITY)
    with psycopg.connect(forge_db) as conn:
        conn.execute(artifacts.ddl)
        conn.execute(artifacts.load_sql)
        conn.commit()
    return ontology, artifacts, forge_db


def _plumbing_labels(ontology: ForgeOntology, entity_name: str) -> set[str]:
    """The generated surrogate columns the forward CSI will report as extra
    conceptual properties: the ``id`` PK plus one FK label per outgoing
    relationship (``account_id`` -> ``accountId``)."""
    from r2g.csi import owl_property_name

    labels = {"id"}
    for r in ontology.relationships:
        if r.from_entity == entity_name:
            labels.add(owl_property_name(foreign_key_column(r.to_entity)))
    return labels


def test_roundtrip_reproduces_the_ontology(loaded_federation, record_property):
    """The D-3 property, end to end through the real pipeline."""
    from r2g.connectors.base import create_source_connector

    ontology, _artifacts, dsn = loaded_federation

    # (1) REAL introspection.
    schema = create_source_connector("postgresql", dsn).get_schema()
    assert set(schema.tables) == {table_name(e.name) for e in ontology.entities}

    # (2) Declared constraints came back declared (F-4): every table has the
    # surrogate PK; every relationship an FK.
    for entity in ontology.entities:
        table = schema.tables[table_name(entity.name)]
        assert table.primary_key == ["id"], f"{entity.name}: PK not recovered"
    fk_pairs = {
        (t_name, fk.foreign_table)
        for t_name, t in schema.tables.items()
        for fk in t.foreign_keys
    }
    expected_fk_pairs = {
        (table_name(r.from_entity), table_name(r.to_entity)) for r in ontology.relationships
    }
    assert fk_pairs == expected_fk_pairs

    # (3) Types roundtrip through the real map (F-3): every conceptual
    # property's column introspects back to the declared JSON type.
    for entity in ontology.entities:
        table = schema.tables[table_name(entity.name)]
        columns = {c.name: c.data_type for c in table.columns}
        for prop in entity.properties:
            got = pg_type_to_json_type(columns[column_name(prop.name)])
            assert got == prop.type, f"{entity.name}.{prop.name}: {got} != {prop.type}"

    # (4) REAL Auto-Map + REAL CSI emitter. label_policy="warn" keeps labels
    # untouched so the comparison is direct; the only tolerated collisions are
    # generated plumbing (the ``id`` surrogate on every entity).
    mapping = ConfigManager.generate_default_config(schema)
    csi = mapping_to_csi(mapping, schema, source_type="postgresql", source_ref="forge", label_policy="warn")
    validate_csi(csi)
    conceptual = csi["conceptualModel"]

    got_entities = {e["name"]: {p["name"] for p in e["properties"]} for e in conceptual["entities"]}
    assert set(got_entities) == {e.name for e in ontology.entities}

    for entity in ontology.entities:
        declared = {p.name for p in entity.properties}
        got = got_entities[entity.name]
        missing = declared - got
        assert not missing, f"{entity.name}: properties lost in roundtrip: {missing}"
        extras = got - declared - _plumbing_labels(ontology, entity.name)
        assert not extras, f"{entity.name}: unexpected extra properties: {extras}"

    got_relationships = {
        (r["type"], r["fromEntity"], r["toEntity"]) for r in conceptual["relationships"]
    }
    expected_relationships = {
        (r.type, r.from_entity, r.to_entity) for r in ontology.relationships
    }
    assert got_relationships == expected_relationships

    # (5) Collisions recorded are plumbing only — business labels stayed
    # collision-free by construction (F-6).
    plumbing = set().union(*(_plumbing_labels(ontology, e.name) for e in ontology.entities))
    collision_labels = {c["label"] for c in csi["provenance"].get("labelCollisions", [])}
    assert collision_labels <= plumbing, f"business-label collisions: {collision_labels - plumbing}"

    record_property("forge_roundtrip_entities", sorted(got_entities))
    record_property("forge_roundtrip_collisions", sorted(collision_labels))


def test_rsa_leg_agrees_on_the_conceptual_shape(loaded_federation):
    """The other real analyzer: RSA's deterministic engine proposes the same
    collections and the same FK edges from the generated schema."""
    from r2g.connectors.base import create_source_connector

    ontology, _artifacts, dsn = loaded_federation
    schema = create_source_connector("postgresql", dsn).get_schema()

    proposal, _meta = propose_ontology_from_schema(schema)
    proposed_tables = {c.source_table for c in proposal.collections}
    assert proposed_tables == {table_name(e.name) for e in ontology.entities}

    proposed_edges = {(e.from_collection, e.to_collection) for e in proposal.edges}
    expected_edges = {
        (table_name(r.from_entity), table_name(r.to_entity)) for r in ontology.relationships
    }
    assert proposed_edges == expected_edges


def test_loaded_data_matches_artifacts_and_spine_joins(loaded_federation):
    """The synthesized rows actually landed, and the join spine holds in SQL —
    the pre-partition dataset expected answers get computed on (F-5)."""
    import psycopg

    ontology, artifacts, dsn = loaded_federation
    with psycopg.connect(dsn) as conn:
        for entity in ontology.entities:
            table = table_name(entity.name)
            count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            assert count == ROWS_PER_ENTITY == len(artifacts.rows[table])
        # Every child joins to a parent — no orphans, by construction.
        orphans = conn.execute(
            "SELECT count(*) FROM contacts c LEFT JOIN accounts a ON c.account_id = a.id "
            "WHERE a.id IS NULL"
        ).fetchone()[0]
        assert orphans == 0
