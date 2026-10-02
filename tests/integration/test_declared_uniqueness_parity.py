"""Declared uniqueness, end to end against a live PostgreSQL (PLAN-key-identification A2).

FK inference only targets candidate keys -- a primary key or a UNIQUE key -- so
whether uniqueness reaches inference decides whether natural-key relationships
are found at all. The broader RSA parity audit could not see this: it normalised
both connectors' output through r2g's serializer, which (before A1) stripped
uniqueness from both sides, and its corpora contain no UNIQUE constraints.

This test builds a throwaway schema holding exactly the three cases that matter
and pins what each side sees today:

- ``accounts.account_code`` -- a UNIQUE *constraint*.
- ``stores.manager_id`` -- uniqueness expressed only as a UNIQUE *index*, with no
  constraint (common in Postgres; pagila's ``store.manager_staff_id`` is one).
- ``orders.account_code`` -- references the natural key, not the surrogate PK.

Two assertions pin known gaps and are *expected to flip*:

- r2g's own connector reads no UNIQUE constraint. Plan B (introspection via RSA)
  closes this; when it does, update the assertion.
- RSA reads no unique *index*. Plan A step A3a closes this; when it does, update
  the assertion.

The end-to-end assertion is the one that must hold now: RSA's output, carried
through r2g's Schema, saved and reloaded, still yields the natural-key FK.
"""

from __future__ import annotations

import os
import uuid

import pytest
from relational_schema_analyzer.connectors.base import (
    create_source_connector as rsa_create_source_connector,
)

from r2g.connectors.base import create_source_connector
from r2g.fk_inference import infer_foreign_keys
from r2g.types import Schema

from .conftest import PG_CONN

pytestmark = pytest.mark.integration


def _psycopg():
    try:
        import psycopg

        return psycopg
    except ImportError:  # pragma: no cover - environment dependent
        pytest.skip("psycopg not installed")


@pytest.fixture
def uniqueness_schema():
    """Create a throwaway schema; always drop it, even if a test fails."""
    psycopg = _psycopg()
    name = f"r2g_uq_{os.getpid()}_{uuid.uuid4().hex[:6]}"
    try:
        conn = psycopg.connect(PG_CONN, autocommit=True)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    try:
        conn.execute(f"CREATE SCHEMA {name}")
        conn.execute(
            f"""
            CREATE TABLE {name}.accounts (
                account_id   integer PRIMARY KEY,
                account_code text NOT NULL,
                CONSTRAINT accounts_code_key UNIQUE (account_code)
            );
            CREATE TABLE {name}.stores (
                store_id   integer PRIMARY KEY,
                manager_id integer NOT NULL
            );
            CREATE UNIQUE INDEX stores_manager_uq ON {name}.stores (manager_id);
            CREATE TABLE {name}.orders (
                order_id     integer PRIMARY KEY,
                account_code text
            );
            """
        )
        yield name
    finally:
        conn.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")
        conn.close()


def _unique(schema, table: str) -> list[list[str]]:
    return [list(u) for u in schema.tables[table].unique_constraints]


def test_rsa_reads_the_unique_constraint(uniqueness_schema):
    rsa = rsa_create_source_connector("postgresql", PG_CONN, uniqueness_schema).get_schema()
    assert _unique(rsa, "accounts") == [["account_code"]]


def test_r2g_connector_reads_no_unique_constraint_yet(uniqueness_schema):
    # Known gap, pinned so it cannot change unnoticed. Plan B (delegating
    # introspection to RSA) closes it -- when it does, this should become
    # == [["account_code"]].
    r2g = create_source_connector("postgresql", PG_CONN, uniqueness_schema).get_schema()
    assert _unique(r2g, "accounts") == []


def test_neither_side_reads_a_unique_index_yet(uniqueness_schema):
    # Known gap, pinned. A unique index with no constraint is a real candidate
    # key that inference cannot see. Plan A step A3a (RSA) closes it.
    rsa = rsa_create_source_connector("postgresql", PG_CONN, uniqueness_schema).get_schema()
    r2g = create_source_connector("postgresql", PG_CONN, uniqueness_schema).get_schema()
    stores_rsa = rsa.tables["stores"]
    assert _unique(rsa, "stores") == []
    assert not any(c.is_unique for c in stores_rsa.columns if c.name == "manager_id")
    assert _unique(r2g, "stores") == []


def test_natural_key_fk_survives_snapshot_persistence(uniqueness_schema, tmp_path):
    """RSA's real introspection, stored through the catalog and reloaded from disk."""
    from r2g.catalog import CatalogManager

    rsa = rsa_create_source_connector("postgresql", PG_CONN, uniqueness_schema).get_schema()
    mgr = CatalogManager(tmp_path)
    mgr.add_source("pg", "postgresql", PG_CONN)
    mgr.create_snapshot("pg", Schema.model_validate(rsa.model_dump()), pg_schema=uniqueness_schema)
    reloaded = CatalogManager(tmp_path).get_latest_snapshot("pg")
    assert reloaded is not None
    schema = reloaded.schema_data

    assert _unique(schema, "accounts") == [["account_code"]]
    inferred = {
        (c.table, tuple(c.columns), c.foreign_table, tuple(c.foreign_columns)) for c in infer_foreign_keys(schema)
    }
    assert ("orders", ("account_code",), "accounts", ("account_code",)) in inferred
