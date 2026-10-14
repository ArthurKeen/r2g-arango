"""`r2g source suggest-keys` on PostgreSQL, end to end against a live database (P6.10).

Two shapes, each in a throwaway schema that is always dropped:

- **keyless** -- a warehouse landing with no primary keys, unique constraints or
  foreign keys. The full loop runs: propose -> review gate -> snapshot.
- **surrogate key** -- every table declares a ``bigserial`` primary key that no child
  references, while children point at an undeclared natural key. This is the shape
  of contextual-data-fabric's CRM source. Before relational-schema-analyzer 0.9.4
  every such table was skipped for "having a key", so nothing was proposed.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest
from typer.testing import CliRunner

from r2g.catalog import CatalogManager
from r2g.main import app

from .conftest import PG_CONN

pytestmark = pytest.mark.integration
runner = CliRunner()


def _psycopg():
    try:
        import psycopg

        return psycopg
    except ImportError:  # pragma: no cover - environment dependent
        pytest.skip("psycopg not installed")


def _quiet_logging(*_args, **_kwargs) -> None:
    import sys

    import structlog

    structlog.reset_defaults()
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(0),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )


@pytest.fixture(autouse=True)
def _cli(tmp_path, monkeypatch):
    """A private catalog, and logging that survives several CliRunner invokes."""
    monkeypatch.setattr("r2g.main.setup_logging", _quiet_logging)
    monkeypatch.setattr("r2g.main._get_catalog", lambda: CatalogManager(str(tmp_path / "catalog")))
    monkeypatch.chdir(tmp_path)
    _quiet_logging()
    yield
    _quiet_logging()


def _schema(ddl: list[str]):
    psycopg = _psycopg()
    name = f"r2g_sk_{os.getpid()}_{uuid.uuid4().hex[:6]}"
    try:
        conn = psycopg.connect(PG_CONN, autocommit=True)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    try:
        conn.execute(f"CREATE SCHEMA {name}")
        for stmt in ddl:
            conn.execute(stmt.format(s=name))
        yield name
    finally:
        conn.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")
        conn.close()


@pytest.fixture
def keyless():
    yield from _schema(
        [
            "CREATE TABLE {s}.accounts (account_id text, name text)",
            "INSERT INTO {s}.accounts SELECT 'A'||g, 'n'||g FROM generate_series(1,200) g",
            "CREATE TABLE {s}.contracts (contract_id text, account_id text)",
            "INSERT INTO {s}.contracts SELECT 'C'||g, 'A'||((g%200)+1) FROM generate_series(1,800) g",
        ]
    )


@pytest.fixture
def surrogate():
    yield from _schema(
        [
            "CREATE TABLE {s}.accounts (id bigserial PRIMARY KEY, account_id text, account_name text)",
            "INSERT INTO {s}.accounts (account_id, account_name) "
            "SELECT 'A'||g, 'name '||g FROM generate_series(1,200) g",
            "CREATE TABLE {s}.contracts (id bigserial PRIMARY KEY, account_id text)",
            "INSERT INTO {s}.contracts (account_id) SELECT 'A'||((g%200)+1) FROM generate_series(1,800) g",
        ]
    )


def _add_source(tmp_path, name="crm"):
    CatalogManager(str(tmp_path / "catalog")).add_source(name, "postgresql", PG_CONN)


def _draft(tmp_path, name="crm"):
    return json.loads((tmp_path / f"{name}.keys.draft.json").read_text())


def test_the_full_loop_on_a_keyless_postgres_schema(keyless, tmp_path):
    _add_source(tmp_path)
    result = runner.invoke(app, ["source", "suggest-keys", "crm", "--schema", keyless])
    assert result.exit_code == 0, result.output
    assert "Cost:" in result.output
    # The snapshot must read the schema the draft was built from.
    assert f"--pg-schema {keyless}" in result.output

    tables = _draft(tmp_path)["tables"]
    assert tables["accounts"]["primaryKey"] == ["account_id"]
    assert tables["contracts"]["primaryKey"] == ["contract_id"]
    (fk,) = tables["contracts"]["foreignKeys"]
    assert fk["references"] == {"table": "accounts", "columns": ["account_id"]}
    assert CatalogManager(str(tmp_path / "catalog")).get_source("crm").key_overlay is None

    attached = runner.invoke(
        app, ["source", "set-key-overlay", "crm", "crm.keys.draft.json", "--reviewed"]
    )
    assert attached.exit_code == 0, attached.output
    snapped = runner.invoke(app, ["source", "snapshot", "crm", "--pg-schema", keyless])
    assert snapped.exit_code == 0, snapped.output
    snap = CatalogManager(str(tmp_path / "catalog")).get_latest_snapshot("crm").schema_data
    assert snap.tables["accounts"].primary_key == ["account_id"]
    (sfk,) = snap.tables["contracts"].foreign_keys
    assert sfk.foreign_table == "accounts" and sfk.enforced is False


def test_a_natural_key_beside_a_surrogate_one_is_proposed(surrogate, tmp_path):
    """CDF's shape: declared surrogate PKs, references to an undeclared natural key."""
    _add_source(tmp_path)
    result = runner.invoke(app, ["source", "suggest-keys", "crm", "--schema", surrogate])
    assert result.exit_code == 0, result.output
    tables = _draft(tmp_path)["tables"]
    # The declared surrogate stays the primary key; the natural key is a UNIQUE key.
    assert "primaryKey" not in tables["accounts"]
    assert ["account_id"] in tables["accounts"]["uniqueConstraints"]
    found = {
        (fk["references"]["table"], tuple(fk["references"]["columns"]))
        for fk in tables["contracts"]["foreignKeys"]
    }
    assert ("accounts", ("account_id",)) in found


def test_the_query_budget_holds_on_postgres(keyless, tmp_path):
    _add_source(tmp_path)
    result = runner.invoke(
        app, ["source", "suggest-keys", "crm", "--schema", keyless, "--max-queries", "1"]
    )
    assert result.exit_code == 0, result.output
    assert "Query budget used up (1 of 1 queries)" in result.output


def test_a_wrong_password_fails_loudly_and_writes_nothing(keyless, tmp_path, monkeypatch):
    """Introspection succeeds; only the sampler's own login fails. Every probe would
    decline, which must not pass for a clean run that found no keys."""
    psycopg = _psycopg()
    _add_source(tmp_path)
    info = psycopg.conninfo.conninfo_to_dict(PG_CONN)
    info["password"] = "definitely-wrong"
    bad_url = psycopg.conninfo.make_conninfo(**info)

    import r2g.main as main

    real = main._key_probe_sampler
    monkeypatch.setattr(main, "_key_probe_sampler", lambda kind, _url, **gov: real(kind, bad_url, **gov))
    result = runner.invoke(app, ["source", "suggest-keys", "crm", "--schema", keyless])
    assert result.exit_code == 1, result.output
    assert "could not connect" in result.output
    assert not (tmp_path / "crm.keys.draft.json").exists()
