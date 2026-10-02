from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from r2g.demo.snowflake_customer_360 import (
    OVERLAY_SOURCE,
    configured_location,
    connection_string,
    load_overlay,
)


def _bootstrap_module():
    path = (
        Path(__file__).parents[1]
        / "examples"
        / "snowflake_customer_360"
        / "bootstrap.py"
    )
    spec = importlib.util.spec_from_file_location("customer360_bootstrap", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bundled_overlay_declares_expected_keys():
    overlay = load_overlay()

    assert OVERLAY_SOURCE.endswith("keys.overlay.json")
    assert set(overlay["tables"]) == {
        "ACCOUNTS",
        "CONTACTS",
        "EMAIL_EVENTS",
        "ZOOM_TELEMETRY",
        "HEALTH_SIGNALS",
    }
    assert sum(
        len(spec.get("foreignKeys", []))
        for spec in overlay["tables"].values()
    ) == 6


def test_connection_string_keeps_credentials_as_environment_references(
    monkeypatch,
):
    monkeypatch.delenv("SNOWFLAKE_PRIVATE_KEY_FILE_PWD", raising=False)

    uri = connection_string()

    assert "$SNOWFLAKE_USER" in uri
    assert "$SNOWFLAKE_ACCOUNT" in uri
    assert "$SNOWFLAKE_PRIVATE_KEY_FILE" in uri
    assert "$SNOWFLAKE_ROLE" in uri
    assert "private_key_file_pwd" not in uri


def test_connection_string_adds_optional_passphrase_reference(monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_PRIVATE_KEY_FILE_PWD", "sentinel")

    uri = connection_string()

    assert "private_key_file_pwd=$SNOWFLAKE_PRIVATE_KEY_FILE_PWD" in uri
    assert "sentinel" not in uri


def test_location_overrides_are_validated(monkeypatch):
    monkeypatch.setenv("R2G_SNOWFLAKE_DEMO_DATABASE", "unsafe;drop")

    with pytest.raises(ValueError, match="Invalid Snowflake demo database"):
        configured_location()


def test_seed_rows_have_unique_keys_and_no_orphan_relationships():
    module = _bootstrap_module()
    rows = module.ROWS

    for table, pk in module.PK_COLUMNS.items():
        # The first column of each demo table is its configured key.
        values = [row[0] for row in rows[table]]
        assert all(values)
        assert len(values) == len(set(values)), f"{table}.{pk}"

    account_ids = {row[0] for row in rows["ACCOUNTS"]}
    contact_ids = {row[0] for row in rows["CONTACTS"]}
    assert {row[1] for row in rows["CONTACTS"]} <= account_ids
    assert {row[1] for row in rows["EMAIL_EVENTS"]} <= contact_ids
    assert {row[2] for row in rows["EMAIL_EVENTS"]} <= account_ids
    assert {row[1] for row in rows["ZOOM_TELEMETRY"]} <= contact_ids
    assert {row[2] for row in rows["ZOOM_TELEMETRY"]} <= account_ids
    assert {row[1] for row in rows["HEALTH_SIGNALS"]} <= account_ids


def test_operator_errors_redact_key_material(monkeypatch):
    module = _bootstrap_module()
    monkeypatch.setenv("SNOWFLAKE_PRIVATE_KEY_FILE", "/secret/key/path.p8")
    monkeypatch.setenv("SNOWFLAKE_PRIVATE_KEY_FILE_PWD", "sentinel-passphrase")

    message = module._safe_operator_error(
        RuntimeError("failed /secret/key/path.p8 sentinel-passphrase")
    )

    assert "/secret/key/path.p8" not in message
    assert "sentinel-passphrase" not in message

