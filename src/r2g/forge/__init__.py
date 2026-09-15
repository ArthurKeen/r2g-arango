"""Federation Forge — the reverse generator (contextual-data-fabric ADR-0006, D-4).

Public seam (the only thing the fabric's orchestration may depend on)::

    generate(ontology, dialect, seed) -> ForgeArtifacts(ddl, load_sql, rows)

The package is split by concern: :mod:`.core` holds the ontology contract,
naming inverses, and dialect-independent synthesis; :mod:`.dialects` holds
the per-system projections behind one registry.
"""

from .core import (
    PG_TYPE_FOR_JSON_TYPE,
    SUPPORTED_DIALECTS,
    ForgeArtifacts,
    ForgeEntity,
    ForgeError,
    ForgeOntology,
    ForgeProperty,
    ForgeRelationship,
    column_name,
    expected_relationship_type,
    foreign_key_column,
    generate,
    load_ontology_file,
    table_name,
)

__all__ = [
    "ForgeArtifacts",
    "ForgeEntity",
    "ForgeError",
    "ForgeOntology",
    "ForgeProperty",
    "ForgeRelationship",
    "PG_TYPE_FOR_JSON_TYPE",
    "SUPPORTED_DIALECTS",
    "column_name",
    "expected_relationship_type",
    "foreign_key_column",
    "generate",
    "load_ontology_file",
    "table_name",
]
