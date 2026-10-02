"""Shared source introspection and snapshot construction.

All catalog-facing entry points use this module so reviewed key overlays and
classifications are applied in the same order in the CLI, Studio, and MCP.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from relational_schema_analyzer.types import PhysicalSchema

from r2g.types import Schema

if TYPE_CHECKING:
    from r2g.catalog import CatalogManager, SchemaSnapshot, SourceConfig


@dataclass(frozen=True)
class SnapshotBuildResult:
    """An introspected schema plus non-secret overlay audit metadata."""

    schema: Schema
    key_overlay_summary: dict[str, int] = field(default_factory=dict)
    key_overlay_source: str | None = None
    key_overlay_fingerprint: str | None = None
    classifications_applied: int = 0


def overlay_fingerprint(overlay: dict[str, Any] | None) -> str | None:
    """Return a stable SHA-256 digest for parsed overlay content."""
    if overlay is None:
        return None
    canonical = json.dumps(
        overlay,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def build_source_schema(
    source: SourceConfig,
    *,
    schema_name: str,
) -> SnapshotBuildResult:
    """Introspect, apply a reviewed key overlay, then add classifications."""
    from r2g.connectors.base import create_source_connector

    connector = create_source_connector(
        source.source_type or "postgresql",
        source.connection_string,
        schema_name=schema_name,
        source_params=source.source_params,
    )
    physical: PhysicalSchema = connector.get_schema()
    summary: dict[str, int] = {}

    if source.key_overlay is not None:
        from relational_schema_analyzer import apply_key_overlay, overlay_summary

        physical = apply_key_overlay(physical, source.key_overlay)
        summary = overlay_summary(physical)

    # Re-validate into r2g's compatibility subclasses before classifications
    # and persistence. ForeignKey provenance (overlay:* / enforced=False)
    # survives; RSA Table.extra is summarized above because r2g omits it.
    schema = Schema.model_validate(physical.model_dump())
    classified = 0
    if source.classifications:
        from r2g.classification import annotate_schema

        classified = annotate_schema(schema, source.classifications)

    return SnapshotBuildResult(
        schema=schema,
        key_overlay_summary=summary,
        key_overlay_source=source.key_overlay_source if summary else None,
        key_overlay_fingerprint=overlay_fingerprint(source.key_overlay),
        classifications_applied=classified,
    )


def capture_source_snapshot(
    catalog: CatalogManager,
    source_name: str,
    *,
    schema_name: str,
) -> tuple[SchemaSnapshot, SnapshotBuildResult]:
    """Build and persist one source snapshot through the shared pipeline."""
    source = catalog.get_source(source_name)
    if source is None:
        raise ValueError(f"Source '{source_name}' not found")
    result = build_source_schema(source, schema_name=schema_name)
    snapshot = catalog.create_snapshot(
        source_name,
        result.schema,
        pg_schema=schema_name,
        key_overlay_summary=result.key_overlay_summary,
        key_overlay_source=result.key_overlay_source,
        key_overlay_fingerprint=result.key_overlay_fingerprint,
    )
    return snapshot, result

