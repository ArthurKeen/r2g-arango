"""Federation Forge — the reverse generator (walking skeleton).

Commissioned by contextual-data-fabric ADR-0006 (D-4) and specified in
``docs/internal/PLAN-federation-forge.md``: from a conceptual ontology,
*generate* a physical schema plus seeded synthetic data, such that running the
REAL forward pipeline over the loaded result reproduces the ontology::

    introspect(generate(O)) == O      (up to CC-12 normalization and plumbing)

The seam this module ships (and the only thing the fabric's orchestration may
depend on)::

    generate(ontology, dialect, seed) -> ForgeArtifacts(ddl, load_sql, rows)

Skeleton scope: the ``postgres`` dialect only; declared PK/FK constraints
always emitted (the constraint-stripped variant is the S3 denormalizer's job);
input ontologies are collision-free by construction (F-6) and are *refused*
otherwise — the forge fails loudly at generate time, never at compare time.
"""

from __future__ import annotations

import json
import random
from typing import Any, Dict, List

from pydantic import BaseModel, Field

from .csi import owl_entity_name, owl_property_name
from .naming import convert_identifier, pluralize

#: Conceptual JSON types the skeleton synthesizes, and the one canonical
#: roundtrip-stable Postgres spelling for each (PLAN F-3): the generated DDL
#: type must introspect back through ``config.pg_type_to_json_type`` to the
#: same JSON type.
PG_TYPE_FOR_JSON_TYPE: Dict[str, str] = {
    "integer": "bigint",
    "float": "double precision",
    "boolean": "boolean",
    "string": "text",
}

#: Dialects the seam accepts today. S2 adds snowflake-sql / clickhouse-sql /
#: arango behind the same signature.
SUPPORTED_DIALECTS = ("postgres",)


class ForgeError(ValueError):
    """A refused ontology or unsupported request — always at generate time."""


class ForgeProperty(BaseModel):
    """One conceptual property: lowerCamel name + JSON type (PLAN F-1)."""

    name: str
    type: str


class ForgeRelationship(BaseModel):
    """One conceptual relationship; ``type`` must equal the name the forward
    pipeline will re-derive (``<from_table>_to_<to_table>`` in lowerCamel), so
    the roundtrip comparison stays exact."""

    type: str
    from_entity: str = Field(alias="fromEntity")
    to_entity: str = Field(alias="toEntity")

    model_config = {"populate_by_name": True}


class ForgeEntity(BaseModel):
    """One conceptual class: singular PascalCase name + typed properties."""

    name: str
    properties: List[ForgeProperty]


class ForgeOntology(BaseModel):
    """A validated conceptual ontology — the forge's only input contract.

    Build one with :meth:`from_conceptual`, which accepts either a bare
    ``{"entities": …, "relationships": …}`` object or a full CSI v1 document
    (the ``conceptualModel`` is taken) and refuses anything the skeleton
    cannot roundtrip byte-honestly.
    """

    entities: List[ForgeEntity]
    relationships: List[ForgeRelationship] = Field(default_factory=list)

    @classmethod
    def from_conceptual(cls, document: Dict[str, Any]) -> "ForgeOntology":
        conceptual = document.get("conceptualModel", document)
        entities = [
            ForgeEntity(
                name=e["name"],
                properties=[
                    ForgeProperty(name=p["name"], type=p.get("type", ""))
                    for p in e.get("properties", [])
                ],
            )
            for e in conceptual.get("entities", [])
        ]
        relationships = [
            ForgeRelationship.model_validate(r)
            for r in conceptual.get("relationships", [])
        ]
        ontology = cls(entities=entities, relationships=relationships)
        ontology.validate_for_forge()
        return ontology

    def entity(self, name: str) -> ForgeEntity:
        for e in self.entities:
            if e.name == name:
                return e
        raise ForgeError(f"unknown entity {name!r}")

    def validate_for_forge(self) -> None:
        """Refuse anything that cannot survive the roundtrip (PLAN F-2/F-6)."""
        if not self.entities:
            raise ForgeError("ontology declares no entities")

        seen_entities: set[str] = set()
        label_owner: Dict[str, str] = {}
        for e in self.entities:
            if e.name in seen_entities:
                raise ForgeError(f"duplicate entity {e.name!r}")
            seen_entities.add(e.name)

            table = table_name(e.name)
            if owl_entity_name(table) != e.name:
                raise ForgeError(
                    f"entity {e.name!r} does not survive the CC-12 naming "
                    f"roundtrip: table {table!r} normalizes back to "
                    f"{owl_entity_name(table)!r}. Rename the class so that "
                    "generate/introspect agree (PLAN F-2)."
                )

            seen_props: set[str] = set()
            for p in e.properties:
                if p.type not in PG_TYPE_FOR_JSON_TYPE:
                    raise ForgeError(
                        f"{e.name}.{p.name}: unsupported type {p.type!r} "
                        f"(skeleton supports {sorted(PG_TYPE_FOR_JSON_TYPE)})"
                    )
                if p.name in seen_props:
                    raise ForgeError(f"duplicate property {e.name}.{p.name}")
                seen_props.add(p.name)
                column = column_name(p.name)
                if owl_property_name(column) != p.name:
                    raise ForgeError(
                        f"property {e.name}.{p.name!r} does not survive the "
                        f"CC-12 naming roundtrip: column {column!r} normalizes "
                        f"back to {owl_property_name(column)!r} (PLAN F-2)."
                    )
                if p.name == "id":
                    raise ForgeError(
                        f"{e.name}.id collides with the generated surrogate "
                        "primary key; declare a domain identifier instead"
                    )
                # Collision-free by construction (F-6): deliberate collisions
                # are an S3 denormalizer feature, not a skeleton input.
                owner = label_owner.get(p.name)
                if owner is not None:
                    raise ForgeError(
                        f"property label {p.name!r} appears on both {owner} "
                        f"and {e.name}; skeleton ontologies must be "
                        "collision-free (PLAN F-6)"
                    )
                label_owner[p.name] = e.name

        seen_edges: set[tuple[str, str]] = set()
        for r in self.relationships:
            if r.from_entity not in seen_entities:
                raise ForgeError(f"relationship {r.type!r}: unknown fromEntity {r.from_entity!r}")
            if r.to_entity not in seen_entities:
                raise ForgeError(f"relationship {r.type!r}: unknown toEntity {r.to_entity!r}")
            if (r.from_entity, r.to_entity) in seen_edges:
                raise ForgeError(
                    f"duplicate relationship {r.from_entity} -> {r.to_entity}; "
                    "the skeleton supports one relationship per entity pair"
                )
            seen_edges.add((r.from_entity, r.to_entity))

            expected = expected_relationship_type(r.from_entity, r.to_entity)
            if r.type != expected:
                raise ForgeError(
                    f"relationship {r.from_entity} -> {r.to_entity} must be "
                    f"named {expected!r} (the name the forward pipeline "
                    f"re-derives), got {r.type!r}"
                )

            fk_column = foreign_key_column(r.to_entity)
            for p in self.entity(r.from_entity).properties:
                if column_name(p.name) == fk_column:
                    raise ForgeError(
                        f"{r.from_entity}.{p.name} collides with the "
                        f"generated foreign-key column {fk_column!r} for "
                        f"relationship {r.type!r}"
                    )


class ForgeArtifacts(BaseModel):
    """What ``generate`` returns: DDL, a loader script, and the synthesized
    rows (the pre-partition dataset expected answers are computed on)."""

    dialect: str
    seed: int
    ddl: str
    load_sql: str
    rows: Dict[str, List[Dict[str, Any]]]

    def write_to(self, out_dir: str) -> List[str]:
        """Write ``forge.sql`` / ``forge.load.sql`` / ``forge.rows.json``
        under ``out_dir`` and return the paths written."""
        import os

        os.makedirs(out_dir, exist_ok=True)
        paths = []
        for filename, content in (
            ("forge.sql", self.ddl),
            ("forge.load.sql", self.load_sql),
            ("forge.rows.json", json.dumps(self.rows, indent=2, sort_keys=True) + "\n"),
        ):
            path = os.path.join(out_dir, filename)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
            paths.append(path)
        return paths


def table_name(entity: str) -> str:
    """CC-12 inverse for classes: singular PascalCase -> plural snake_case."""
    return pluralize(convert_identifier(entity, "snake"))


def column_name(prop: str) -> str:
    """CC-12 inverse for properties: lowerCamel -> snake_case."""
    return convert_identifier(prop, "snake")


def foreign_key_column(to_entity: str) -> str:
    """The generated FK column pointing at ``to_entity``: ``account_id``."""
    return f"{convert_identifier(to_entity, 'snake')}_id"


def expected_relationship_type(from_entity: str, to_entity: str) -> str:
    """The relationship name the forward pipeline re-derives for an FK:
    Auto-Map names the edge ``<from_table>_to_<to_table>`` and the CSI emitter
    lowerCamels it."""
    edge = f"{table_name(from_entity)}_to_{table_name(to_entity)}"
    return owl_property_name(edge)


def _topological_entity_order(ontology: ForgeOntology) -> List[str]:
    """Parents before children (FK targets first), deterministic tie-break by
    name. Cycles are refused — the skeleton generates trees/DAGs only."""
    names = sorted(e.name for e in ontology.entities)
    depends_on: Dict[str, set[str]] = {n: set() for n in names}
    for r in ontology.relationships:
        depends_on[r.from_entity].add(r.to_entity)

    ordered: List[str] = []
    placed: set[str] = set()
    while len(ordered) < len(names):
        progress = False
        for n in names:
            if n in placed or not depends_on[n] <= placed:
                continue
            ordered.append(n)
            placed.add(n)
            progress = True
        if not progress:
            cyclic = sorted(set(names) - placed)
            raise ForgeError(f"relationship cycle among {cyclic}; the skeleton generates DAGs only")
    return ordered


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def _synthesize_value(rng: random.Random, json_type: str, prop: str) -> Any:
    if json_type == "integer":
        return rng.randrange(0, 100_000)
    if json_type == "float":
        return round(rng.uniform(0, 100_000), 6)
    if json_type == "boolean":
        return rng.random() < 0.5
    return f"{prop}-{rng.randrange(0, 100_000):05d}"


def generate(
    ontology: ForgeOntology,
    dialect: str = "postgres",
    seed: int = 0,
    *,
    rows_per_entity: int = 10,
) -> ForgeArtifacts:
    """The ADR-0006 D-4 seam: ontology -> ``{ddl, loader, rows}``.

    Deterministic end to end (PLAN F-5): the same ``(ontology, dialect, seed,
    rows_per_entity)`` reproduces byte-identical artifacts. Every FK value is
    drawn from the already-synthesized parent ids, so the join spine agrees
    across tables by construction.
    """
    if dialect not in SUPPORTED_DIALECTS:
        raise ForgeError(
            f"dialect {dialect!r} is not supported yet (skeleton supports "
            f"{list(SUPPORTED_DIALECTS)}; S2 adds the rest)"
        )
    if rows_per_entity < 1:
        raise ForgeError("rows_per_entity must be >= 1")
    ontology.validate_for_forge()

    order = _topological_entity_order(ontology)
    fk_columns: Dict[str, List[tuple[str, str]]] = {e.name: [] for e in ontology.entities}
    for r in ontology.relationships:
        fk_columns[r.from_entity].append((foreign_key_column(r.to_entity), table_name(r.to_entity)))
    for cols in fk_columns.values():
        cols.sort()

    # The schema is a pure function of the ontology — the seed shapes data
    # only, so it is stamped on the loader, never on the DDL.
    ddl_parts: List[str] = [
        "-- Federation Forge — generated schema (walking skeleton)",
        f"-- dialect: {dialect}",
        "-- Regenerate with: r2g forge generate (see PLAN-federation-forge.md)",
        "",
    ]
    for name in order:
        entity = ontology.entity(name)
        table = table_name(name)
        lines = ["    id bigint NOT NULL"]
        for fk_col, _parent in fk_columns[name]:
            lines.append(f"    {fk_col} bigint NOT NULL")
        for p in entity.properties:
            lines.append(f"    {column_name(p.name)} {PG_TYPE_FOR_JSON_TYPE[p.type]}")
        lines.append("    PRIMARY KEY (id)")
        for fk_col, parent_table in fk_columns[name]:
            lines.append(
                f"    FOREIGN KEY ({fk_col}) REFERENCES {parent_table} (id)"
            )
        body = ",\n".join(lines)
        ddl_parts.append(f"CREATE TABLE {table} (\n{body}\n);\n")
    ddl = "\n".join(ddl_parts)

    rng = random.Random(seed)
    rows: Dict[str, List[Dict[str, Any]]] = {}
    for name in order:
        entity = ontology.entity(name)
        table = table_name(name)
        table_rows: List[Dict[str, Any]] = []
        for i in range(1, rows_per_entity + 1):
            row: Dict[str, Any] = {"id": i}
            for fk_col, parent_table in fk_columns[name]:
                parent_ids = [r["id"] for r in rows[parent_table]]
                row[fk_col] = rng.choice(parent_ids)
            for p in entity.properties:
                row[column_name(p.name)] = _synthesize_value(rng, p.type, p.name)
            table_rows.append(row)
        rows[table] = table_rows

    load_parts: List[str] = [
        "-- Federation Forge — generated data (walking skeleton)",
        f"-- dialect: {dialect}; seed: {seed}",
        "",
    ]
    for name in order:
        table = table_name(name)
        for row in rows[table]:
            columns = ", ".join(row.keys())
            values = ", ".join(_sql_literal(v) for v in row.values())
            load_parts.append(f"INSERT INTO {table} ({columns}) VALUES ({values});")
        load_parts.append("")
    load_sql = "\n".join(load_parts)

    return ForgeArtifacts(dialect=dialect, seed=seed, ddl=ddl, load_sql=load_sql, rows=rows)


def load_ontology_file(path: str) -> ForgeOntology:
    """Read a conceptual ontology (bare or full CSI v1 document) from JSON.

    Unreadable or malformed input is a caller problem and raises
    :class:`ForgeError` (never a bare ``OSError``), so the CLI reports it as a
    refusal with the reason attached.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            try:
                document = json.load(fh)
            except json.JSONDecodeError as exc:
                raise ForgeError(f"{path} is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise ForgeError(f"cannot read ontology file {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ForgeError(f"{path} must contain a JSON object")
    return ForgeOntology.from_conceptual(document)


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
