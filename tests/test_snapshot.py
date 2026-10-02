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

