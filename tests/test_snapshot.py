from __future__ import annotations

import pytest

from r2g.catalog import CatalogManager
from r2g.snapshot import (
    build_source_schema,
    capture_source_snapshot,
    overlay_fingerprint,
)
from r2g.types import Column, Schema, Table


@pytest.fixture
def keyless_schema() -> Schema:
    return Schema(
        tables={
            "ACCOUNTS": Table(
                name="ACCOUNTS",
                columns=[
                    Column(name="ACCOUNT_ID", data_type="number", is_nullable=False),
                    Column(name="NAME", data_type="text"),
                ],
            ),
            "CONTACTS": Table(
                name="CONTACTS",
                columns=[
                    Column(name="CONTACT_ID", data_type="number", is_nullable=False),
                    Column(name="ACCOUNT_ID", data_type="number", is_nullable=False),
                ],
            ),
        }
    )


@pytest.fixture
def key_overlay() -> dict:
    return {
        "version": 1,
        "description": "Reviewed Customer 360 keys",
        "tables": {
            "ACCOUNTS": {"primaryKey": ["ACCOUNT_ID"]},
            "CONTACTS": {
                "primaryKey": ["CONTACT_ID"],
                "foreignKeys": [
                    {
                        "columns": ["ACCOUNT_ID"],
                        "references": {
                            "table": "ACCOUNTS",
                            "columns": ["ACCOUNT_ID"],
                        },
                        "constraintName": "contacts_account",
                    }
                ],
            },
        },
    }


class _Connector:
    def __init__(self, schema: Schema) -> None:
        self.schema = schema

    def get_schema(self) -> Schema:
        return self.schema


def test_build_source_schema_applies_overlay_before_persistence(
    monkeypatch, tmp_path, keyless_schema, key_overlay
):
    mgr = CatalogManager(tmp_path)
    source = mgr.add_source(
        "customer360",
        "snowflake",
        "snowflake://user:password@account/DB/SCHEMA",
        key_overlay=key_overlay,
        key_overlay_source="bundled:customer360",
    )
    monkeypatch.setattr(
        "r2g.connectors.base.create_source_connector",
        lambda *args, **kwargs: _Connector(keyless_schema),
    )

    result = build_source_schema(source, schema_name="R2G_CUSTOMER_360")

    assert result.key_overlay_summary == {
        "tables": 2,
        "primaryKeys": 2,
        "foreignKeys": 1,
        "uniqueConstraints": 0,
    }
    assert keyless_schema.tables["ACCOUNTS"].primary_key == []
    assert result.schema.tables["ACCOUNTS"].primary_key == ["ACCOUNT_ID"]
    (fk,) = result.schema.tables["CONTACTS"].foreign_keys
    assert fk.constraint_name == "overlay:contacts_account"
    assert fk.enforced is False


def test_capture_source_snapshot_retains_overlay_audit_metadata(
    monkeypatch, tmp_path, keyless_schema, key_overlay
):
    mgr = CatalogManager(tmp_path)
    mgr.add_source(
        "customer360",
        "snowflake",
        "snowflake://user:password@account/DB/SCHEMA",
        key_overlay=key_overlay,
        key_overlay_source="bundled:customer360",
    )
    monkeypatch.setattr(
        "r2g.connectors.base.create_source_connector",
        lambda *args, **kwargs: _Connector(keyless_schema),
    )

    snapshot, _ = capture_source_snapshot(
        mgr,
        "customer360",
        schema_name="R2G_CUSTOMER_360",
    )
    reloaded = CatalogManager(tmp_path).get_latest_snapshot("customer360")

    assert reloaded is not None
    assert reloaded.id == snapshot.id
    assert reloaded.key_overlay_source == "bundled:customer360"
    assert reloaded.key_overlay_summary["foreignKeys"] == 1
    assert reloaded.key_overlay_fingerprint == overlay_fingerprint(key_overlay)
    (fk,) = reloaded.schema_data.tables["CONTACTS"].foreign_keys
    assert fk.constraint_name == "overlay:contacts_account"
    assert fk.enforced is False


def test_invalid_overlay_fails_snapshot_loudly(
    monkeypatch, tmp_path, keyless_schema
):
    mgr = CatalogManager(tmp_path)
    source = mgr.add_source(
        "customer360",
        "snowflake",
        "snowflake://user:password@account/DB/SCHEMA",
        key_overlay={
            "version": 1,
            "tables": {"ACCOUNTS": {"primaryKey": ["MISSPELLED"]}},
        },
    )
    monkeypatch.setattr(
        "r2g.connectors.base.create_source_connector",
        lambda *args, **kwargs: _Connector(keyless_schema),
    )

    with pytest.raises(ValueError, match="unknown column"):
        build_source_schema(source, schema_name="R2G_CUSTOMER_360")


def test_overlay_fingerprint_is_canonical_and_detects_same_count_changes(
    key_overlay,
):
    reordered = {
        "tables": key_overlay["tables"],
        "description": key_overlay["description"],
        "version": key_overlay["version"],
    }
    changed = {
        **key_overlay,
        "description": "A different reviewed overlay with the same key counts",
    }

    assert overlay_fingerprint(reordered) == overlay_fingerprint(key_overlay)
    assert overlay_fingerprint(changed) != overlay_fingerprint(key_overlay)



# ── Uniqueness must survive persistence (PLAN-key-identification A1) ─────────
#
# FK inference only targets declared candidate keys -- a primary key or a UNIQUE
# constraint. r2g's Table serializer used to drop unique_constraints, so a UNIQUE
# key present at capture (including one a curator declared in a reviewed overlay)
# vanished when the catalog reloaded the snapshot, taking its inferred FK with it.


@pytest.fixture
def natural_key_schema() -> Schema:
    """ORDERS references ACCOUNTS by a natural key, not by the surrogate PK."""
    return Schema(
        tables={
            "ACCOUNTS": Table(
                name="ACCOUNTS",
                columns=[
                    Column(name="ACCOUNT_ID", data_type="number", is_nullable=False),
                    Column(name="ACCOUNT_CODE", data_type="text", is_nullable=False),
                ],
            ),
            "ORDERS": Table(
                name="ORDERS",
                columns=[
                    Column(name="ORDER_ID", data_type="number", is_nullable=False),
                    Column(name="ACCOUNT_CODE", data_type="text"),
                ],
            ),
        }
    )


def _capture_with_overlay(monkeypatch, tmp_path, schema: Schema, overlay: dict):
    mgr = CatalogManager(tmp_path)
    mgr.add_source(
        "warehouse",
        "snowflake",
        "snowflake://user:password@account/DB/SCHEMA",
        key_overlay=overlay,
        key_overlay_source="test:natural-key",
    )
    monkeypatch.setattr(
        "r2g.connectors.base.create_source_connector",
        lambda *args, **kwargs: _Connector(schema),
    )
    capture_source_snapshot(mgr, "warehouse", schema_name="S")
    reloaded = CatalogManager(tmp_path).get_latest_snapshot("warehouse")
    assert reloaded is not None
    return reloaded


def test_overlay_unique_key_survives_reload_and_still_drives_inference(monkeypatch, tmp_path, natural_key_schema):
    from r2g.fk_inference import infer_foreign_keys

    overlay = {
        "version": 1,
        "tables": {
            "ACCOUNTS": {"primaryKey": ["ACCOUNT_ID"], "uniqueConstraints": [["ACCOUNT_CODE"]]},
            "ORDERS": {"primaryKey": ["ORDER_ID"]},
        },
    }
    reloaded = _capture_with_overlay(monkeypatch, tmp_path, natural_key_schema, overlay)

    assert reloaded.schema_data.tables["ACCOUNTS"].unique_constraints == [["ACCOUNT_CODE"]]
    inferred = {
        (c.table, tuple(c.columns), c.foreign_table, tuple(c.foreign_columns))
        for c in infer_foreign_keys(reloaded.schema_data)
    }
    # The only route to this FK is the UNIQUE natural key: ACCOUNT_CODE is not the PK.
    assert ("ORDERS", ("ACCOUNT_CODE",), "ACCOUNTS", ("ACCOUNT_CODE",)) in inferred


def test_new_snapshots_declare_that_uniqueness_was_recorded(monkeypatch, tmp_path, natural_key_schema):
    from r2g.catalog import CURRENT_SCHEMA_FORMAT

    overlay = {"version": 1, "tables": {"ACCOUNTS": {"primaryKey": ["ACCOUNT_ID"]}}}
    reloaded = _capture_with_overlay(monkeypatch, tmp_path, natural_key_schema, overlay)

    # Format 2 means an empty unique_constraints list is a statement ("none"),
    # not an absence of information.
    assert CURRENT_SCHEMA_FORMAT == 2
    assert reloaded.schema_format_version == CURRENT_SCHEMA_FORMAT


def test_legacy_snapshot_reads_as_format_1_and_resaves_byte_identically():
    from datetime import datetime, timezone

    from r2g.catalog import SchemaSnapshot

    legacy = {
        "id": "s1",
        "source_name": "pg",
        "schema_data": {"tables": {}},
        "captured_at": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
        "pg_schema": "public",
        "key_overlay_summary": {},
        "key_overlay_source": None,
        "key_overlay_fingerprint": None,
    }
    snap = SchemaSnapshot.model_validate(legacy)

    # A snapshot written before format 2 never recorded uniqueness, so its empty
    # unique_constraints must not be read as "this table has no unique keys".
    assert snap.schema_format_version == 1
    assert "schema_format_version" not in snap.model_dump(mode="json")


def test_table_without_uniqueness_serializes_its_historical_keys_only():
    table = Table(name="T", columns=[Column(name="ID", data_type="int")])
    assert list(table.model_dump()) == [
        "name",
        "columns",
        "primary_key",
        "foreign_keys",
        "is_partitioned",
        "partition_of",
    ]
