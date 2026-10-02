from __future__ import annotations

import json
import os
import shlex
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from r2g.config import pg_type_to_json_type
from r2g.log import get_logger
from r2g.security import scrub_dsn_credentials
from r2g.types import CollectionMapping, MappingConfig, Schema

logger = get_logger(__name__)

_VALID_ON_DUPLICATE = frozenset({"error", "update", "replace", "ignore"})


def _bash_env_default(var_name: str, value: str) -> str:
    if value == "":
        return f'{var_name}="${{{var_name}:-}}"'
    return f'{var_name}="${{{var_name}:-{shlex.quote(value)}}}"'


def _collection_mapping_for_ref(
    config: MappingConfig,
    name: str,
) -> CollectionMapping | None:
    for key, mapping in config.collections.items():
        if name in (key, mapping.source_table, mapping.target_collection):
            return mapping
    return None


def _source_table_for_ref(config: MappingConfig, name: str) -> str:
    mapping = _collection_mapping_for_ref(config, name)
    return mapping.source_table if mapping is not None else name


def _target_collection_for_ref(config: MappingConfig, name: str) -> str:
    mapping = _collection_mapping_for_ref(config, name)
    return mapping.target_collection if mapping is not None else name


def _script_environment(
    *,
    endpoint: str,
    database: str,
    username: str,
    password: str,
    secret_safe: bool,
) -> list[str]:
    return [
        _bash_env_default(
            "ARANGO_ENDPOINT",
            scrub_dsn_credentials(endpoint) if secret_safe else endpoint,
        ),
        _bash_env_default("ARANGO_DB", database),
        _bash_env_default("ARANGO_USER", username),
        _bash_env_default("ARANGO_PASSWORD", "" if secret_safe else password),
    ]


def _build_jsonl_import_command(
    *,
    endpoint: str,
    database: str,
    username: str,
    password: str,
    file_path: str,
    collection_name: str,
    collection_type: str,
    create_collection: bool,
    create_collection_type: str,
    on_duplicate: str,
    overwrite: bool,
    threads: int,
    environment_credentials: bool,
) -> str:
    resolved_type = (
        create_collection_type
        or ("edge" if collection_type == "edge" else "document")
    )
    server_values = (
        ('"$ARANGO_ENDPOINT"', '"$ARANGO_DB"', '"$ARANGO_USER"', '"$ARANGO_PASSWORD"')
        if environment_credentials
        else tuple(
            shlex.quote(value)
            for value in (endpoint, database, username, password)
        )
    )
    parts: list[str] = [
        "arangoimport",
        "--server.endpoint",
        server_values[0],
        "--server.database",
        server_values[1],
        "--server.username",
        server_values[2],
        "--server.password",
        server_values[3],
        "--file",
        shlex.quote(file_path),
        "--type",
        "jsonl",
        "--collection",
        shlex.quote(collection_name),
        "--create-collection",
        "true" if create_collection else "false",
        "--create-collection-type",
        resolved_type,
        "--on-duplicate",
        shlex.quote(on_duplicate),
        "--threads",
        str(threads),
    ]
    if overwrite:
        parts.append("--overwrite")
    return " ".join(parts)


def _build_graph_creation_arangosh(
    config: MappingConfig,
    graph_name: str,
) -> list[str]:
    """Build an idempotent, shell-safe named-graph creation command."""
    rel_parts: list[str] = []
    for edge in config.edges:
        edge_collection = json.dumps(edge.edge_collection)
        from_collection = json.dumps(
            _target_collection_for_ref(config, edge.from_collection)
        )
        to_collection = json.dumps(
            _target_collection_for_ref(config, edge.to_collection)
        )
        rel_parts.append(
            "  graph._relation("
            f"{edge_collection}, [{from_collection}], [{to_collection}])"
        )

    js_lines = [
        'var graph = require("@arangodb/general-graph");',
        f"try {{ graph._drop({json.dumps(graph_name)}, true); }} catch(e) {{}}",
        "var edgeDefs = [",
        ",\n".join(rel_parts),
        "];",
        f"graph._create({json.dumps(graph_name)}, edgeDefs);",
        f"print({json.dumps(f'Named graph {graph_name} created.')});",
    ]
    js_code = " ".join(js_lines)
    return [
        "echo " + shlex.quote(f"Creating named graph {graph_name}..."),
        (
            "arangosh"
            ' --server.endpoint "$ARANGO_ENDPOINT"'
            ' --server.database "$ARANGO_DB"'
            ' --server.username "$ARANGO_USER"'
            ' --server.password "$ARANGO_PASSWORD"'
            f" --javascript.execute-string {shlex.quote(js_code)}"
        ),
    ]


def _render_graph_shell(
    config: MappingConfig,
    graph_name: str,
    *,
    endpoint: str,
    database: str,
    username: str,
    password: str,
    secret_safe: bool,
) -> str:
    lines = [
        "#!/usr/bin/env bash",
        "# Generated by R2G for arangosh",
        "set -euo pipefail",
        "",
        *_script_environment(
            endpoint=endpoint,
            database=database,
            username=username,
            password=password,
            secret_safe=secret_safe,
        ),
        "",
        *_build_graph_creation_arangosh(config, graph_name),
    ]
    return "\n".join(lines).rstrip() + "\n"


@dataclass(frozen=True)
class ImportCommandSpec:
    """One generated command tied to a validated mapping object."""

    mapping_id: str
    kind: Literal["document", "edge"]
    source_table: str
    target_collection: str
    command: str
    edge_collection: str | None = None
    from_collection: str | None = None
    to_collection: str | None = None
    from_fields: tuple[str, ...] = ()
    to_fields: tuple[str, ...] = ()

    def model_dump(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ImportPlan:
    """Structured, in-memory batch artifact shared by CLI and Studio."""

    mode: Literal["jsonl", "csv"]
    data_dir: str
    script: str
    documents: tuple[ImportCommandSpec, ...]
    edges: tuple[ImportCommandSpec, ...]
    graph_script: str
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def model_dump(self) -> dict:
        """Pydantic-like serializer used by the FastAPI response layer."""
        return asdict(self)


class ArangoImportGenerator:
    """Generates arangoimport shell scripts for loading JSONL data into ArangoDB."""

    def __init__(
        self,
        config: MappingConfig,
        endpoint: str = "http://localhost:8529",
        database: str = "_system",
        username: str = "root",
        password: str = "",
        data_dir: str = "./output",
        on_duplicate: str = "replace",
    ) -> None:
        if on_duplicate not in _VALID_ON_DUPLICATE:
            raise ValueError(
                f"on_duplicate must be one of {sorted(_VALID_ON_DUPLICATE)}, got {on_duplicate!r}"
            )
        self.config = config
        self.endpoint = endpoint
        self.database = database
        self.username = username
        self.password = password
        self.data_dir = data_dir
        self.on_duplicate = on_duplicate

    def build_document_specs(
        self,
        *,
        overwrite: bool = False,
    ) -> tuple[ImportCommandSpec, ...]:
        """Return env-based JSONL commands with stable mapping identities."""
        specs: list[ImportCommandSpec] = []
        for key, mapping in self.config.collections.items():
            if mapping.collection_type != "document":
                continue
            file_path = (
                f"{self.data_dir.rstrip('/')}/"
                f"{mapping.target_collection}.jsonl"
            )
            specs.append(
                ImportCommandSpec(
                    mapping_id=key,
                    kind="document",
                    source_table=mapping.source_table,
                    target_collection=mapping.target_collection,
                    command=self._build_import_command_for_script(
                        mapping.target_collection,
                        file_path,
                        collection_type="document",
                        create_collection_type="document",
                        overwrite=overwrite,
                    ),
                )
            )
        return tuple(specs)

    def build_edge_specs(
        self,
        *,
        overwrite: bool = False,
    ) -> tuple[ImportCommandSpec, ...]:
        """Return env-based JSONL edge commands with mapping metadata."""
        specs: list[ImportCommandSpec] = []
        for edge in self.config.edges:
            file_path = (
                f"{self.data_dir.rstrip('/')}/{edge.edge_collection}.jsonl"
            )
            specs.append(
                ImportCommandSpec(
                    mapping_id=edge.edge_collection,
                    kind="edge",
                    source_table=_source_table_for_ref(
                        self.config,
                        edge.from_collection
                    ),
                    target_collection=edge.edge_collection,
                    edge_collection=edge.edge_collection,
                    from_collection=_target_collection_for_ref(
                        self.config,
                        edge.from_collection
                    ),
                    to_collection=_target_collection_for_ref(
                        self.config,
                        edge.to_collection
                    ),
                    from_fields=tuple(edge.from_fields),
                    to_fields=tuple(edge.to_fields),
                    command=self._build_import_command_for_script(
                        edge.edge_collection,
                        file_path,
                        collection_type="edge",
                        create_collection_type="edge",
                        overwrite=overwrite,
                    ),
                )
            )
        return tuple(specs)

    def _build_import_command(
        self,
        collection_name: str,
        file_path: str,
        collection_type: str = "document",
        create_collection: bool = True,
        create_collection_type: str = "",
        overwrite: bool = False,
        threads: int = 4,
    ) -> str:
        """Build a single arangoimport command string."""
        return _build_jsonl_import_command(
            endpoint=self.endpoint,
            database=self.database,
            username=self.username,
            password=self.password,
            file_path=file_path,
            collection_name=collection_name,
            collection_type=collection_type,
            create_collection=create_collection,
            create_collection_type=create_collection_type,
            on_duplicate=self.on_duplicate,
            overwrite=overwrite,
            threads=threads,
            environment_credentials=False,
        )

    def _build_import_command_for_script(
        self,
        collection_name: str,
        file_path: str,
        collection_type: str = "document",
        create_collection: bool = True,
        create_collection_type: str = "",
        overwrite: bool = False,
        threads: int = 4,
    ) -> str:
        return _build_jsonl_import_command(
            endpoint=self.endpoint,
            database=self.database,
            username=self.username,
            password=self.password,
            file_path=file_path,
            collection_name=collection_name,
            collection_type=collection_type,
            create_collection=create_collection,
            create_collection_type=create_collection_type,
            on_duplicate=self.on_duplicate,
            overwrite=overwrite,
            threads=threads,
            environment_credentials=True,
        )

    def generate_document_commands(self) -> list[str]:
        """Generate import commands for all document collections."""
        base = Path(self.data_dir)
        commands: list[str] = []
        for mapping in self.config.collections.values():
            if mapping.collection_type != "document":
                continue
            file_path = str(base / f"{mapping.target_collection}.jsonl")
            commands.append(
                self._build_import_command(
                    mapping.target_collection,
                    file_path,
                    collection_type="document",
                    create_collection_type="document",
                )
            )
        return commands

    def generate_edge_commands(self) -> list[str]:
        """Generate import commands for all edge collections."""
        base = Path(self.data_dir)
        commands: list[str] = []
        for edge in self.config.edges:
            file_path = str(base / f"{edge.edge_collection}.jsonl")
            commands.append(
                self._build_import_command(
                    edge.edge_collection,
                    file_path,
                    collection_type="edge",
                    create_collection_type="edge",
                )
            )
        return commands

    def render_script(
        self,
        *,
        overwrite_on_initial: bool = True,
        graph_name: str | None = None,
        secret_safe: bool = False,
        generated_at: str | None = None,
    ) -> str:
        """Render a bash script without writing it to disk.

        ``secret_safe`` removes the password default while retaining the
        ``$ARANGO_PASSWORD`` runtime reference used by every command.
        """
        documents = self.build_document_specs(overwrite=overwrite_on_initial)
        edges = self.build_edge_specs(overwrite=overwrite_on_initial)
        return self._render_script(
            documents,
            edges,
            graph_name=graph_name,
            secret_safe=secret_safe,
            generated_at=generated_at,
        )

    def _render_script(
        self,
        documents: tuple[ImportCommandSpec, ...],
        edges: tuple[ImportCommandSpec, ...],
        *,
        graph_name: str | None,
        secret_safe: bool,
        generated_at: str | None = None,
    ) -> str:
        """Render a JSONL script from already-built command specifications."""
        generated_at = generated_at or datetime.now(timezone.utc).isoformat()
        lines: list[str] = [
            "#!/usr/bin/env bash",
            "# Generated by R2G ArangoImportGenerator",
            f"# {generated_at}",
            "set -euo pipefail",
            "",
            *_script_environment(
                endpoint=self.endpoint,
                database=self.database,
                username=self.username,
                password=self.password,
                secret_safe=secret_safe,
            ),
            "",
        ]
        for spec in documents:
            lines.append(
                f"# {spec.source_table} -> {spec.target_collection}"
            )
            lines.append(
                "echo "
                + shlex.quote(
                    f"Importing document collection {spec.target_collection}..."
                )
            )
            lines.append(spec.command)
            lines.append("")
        for spec in edges:
            lines.append(
                f"# {spec.source_table} -> {spec.edge_collection} "
                f"({', '.join(spec.from_fields)} -> {spec.to_collection})"
            )
            lines.append(
                "echo "
                + shlex.quote(
                    f"Importing edge collection {spec.edge_collection}..."
                )
            )
            lines.append(spec.command)
            lines.append("")
        if graph_name:
            lines.extend(["# === Named graph ===", ""])
            lines.extend(
                _build_graph_creation_arangosh(self.config, graph_name)
            )
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def build_plan(
        self,
        *,
        graph_name: str = "r2g_graph",
        overwrite_on_initial: bool = True,
        secret_safe: bool = True,
    ) -> ImportPlan:
        """Build a structured JSONL import artifact for CLI/UI consumers."""
        if self.config.graph_layout == "lpg":
            raise ValueError(
                "arangoimport preview does not support LPG layout; use the "
                "Studio streaming Load path for this mapping"
            )
        documents = self.build_document_specs(overwrite=overwrite_on_initial)
        edges = self.build_edge_specs(overwrite=overwrite_on_initial)
        return ImportPlan(
            mode="jsonl",
            data_dir=self.data_dir,
            script=self._render_script(
                documents,
                edges,
                graph_name=graph_name,
                secret_safe=secret_safe,
            ),
            documents=documents,
            edges=edges,
            graph_script=_render_graph_shell(
                self.config,
                graph_name,
                endpoint=self.endpoint,
                database=self.database,
                username=self.username,
                password=self.password,
                secret_safe=secret_safe,
            ),
        )

    def generate_script(
        self, output_path: str, overwrite_on_initial: bool = True
    ) -> str:
        """Render, write, and make executable an arangoimport bash script."""
        content = self.render_script(
            overwrite_on_initial=overwrite_on_initial,
            secret_safe=False,
        )
        out = Path(output_path)
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(content, encoding="utf-8")
            os.chmod(out, 0o755)
        except OSError as e:
            logger.exception(
                "failed_to_write_arangoimport_script",
                output_path=str(out),
                error=str(e),
            )
            raise
        return content

    def generate_create_graph_aql(self, graph_name: str = "r2g_graph") -> str:
        """Generate arangosh JavaScript to create a named graph from edge definitions."""
        lines = [
            "// Generated for arangosh: create named graph from R2G edge definitions",
            'var graph = require("@arangodb/general-graph");',
            "var edgeDefinitions = [",
        ]
        rel_parts: list[str] = []
        for edge in self.config.edges:
            ec = json.dumps(edge.edge_collection)
            fc = json.dumps(
                _target_collection_for_ref(self.config, edge.from_collection)
            )
            tc = json.dumps(
                _target_collection_for_ref(self.config, edge.to_collection)
            )
            rel_parts.append(f"  graph._relation({ec}, [{fc}], [{tc}])")
        lines.append(",\n".join(rel_parts))
        lines.append("];")
        lines.append(f"graph._create({json.dumps(graph_name)}, edgeDefinitions);")
        return "\n".join(lines) + "\n"


_PG_TO_ARANGO_DATATYPE = {
    "integer": "number",
    "float": "number",
    "boolean": "boolean",
}


class CsvImportGenerator:
    """Generates arangoimport commands that work directly on PG CSV dumps.

    Uses --type csv with --translate, --datatype, --from-collection-prefix,
    --to-collection-prefix, and --remove-attribute to let arangoimport handle
    key remapping and edge projection natively -- no intermediate JSONL needed.
    """

    def __init__(
        self,
        config: MappingConfig,
        schema: Schema,
        endpoint: str = "http://localhost:8529",
        database: str = "_system",
        username: str = "root",
        password: str = "",
        data_dir: str = "./dumps",
        on_duplicate: str = "replace",
    ) -> None:
        if on_duplicate not in _VALID_ON_DUPLICATE:
            raise ValueError(
                f"on_duplicate must be one of {sorted(_VALID_ON_DUPLICATE)}, got {on_duplicate!r}"
            )
        self.config = config
        self.schema = schema
        self.endpoint = endpoint
        self.database = database
        self.username = username
        self.password = password
        self.data_dir = data_dir
        self.on_duplicate = on_duplicate

    def _datatype_flags(
        self, table_name: str, exclude_cols: set[str] | None = None,
    ) -> list[str]:
        """Build --datatype flags from the schema's column types.

        Columns in *exclude_cols* are skipped -- use this for PK/FK columns
        that will become _key, _from, or _to (always strings in ArangoDB).

        Nullable columns are also skipped for number/boolean because
        arangoimport cannot coerce an empty CSV value to those types.
        """
        if table_name not in self.schema.tables:
            return []
        skip = exclude_cols or set()
        flags: list[str] = []
        for col in self.schema.tables[table_name].columns:
            if col.name in skip:
                continue
            json_type = pg_type_to_json_type(col.data_type)
            arango_dt = _PG_TO_ARANGO_DATATYPE.get(json_type)
            if arango_dt and not (col.is_nullable and arango_dt in ("number", "boolean")):
                flags.extend(["--datatype", shlex.quote(f"{col.name}={arango_dt}")])
        return flags

    def _csv_file_path(self, table_name: str) -> str:
        return f"{self.data_dir.rstrip('/')}/{table_name}.csv"

    def _server_flags_for_script(self) -> list[str]:
        return [
            "--server.endpoint", '"$ARANGO_ENDPOINT"',
            "--server.database", '"$ARANGO_DB"',
            "--server.username", '"$ARANGO_USER"',
            "--server.password", '"$ARANGO_PASSWORD"',
        ]

    def _build_doc_command(
        self,
        table_name: str,
        target_collection: str,
        *,
        field_mappings: dict[str, str] | None = None,
        overwrite: bool = True,
    ) -> str:
        """Build arangoimport command for a document collection from a CSV dump."""
        table = self.schema.tables.get(table_name)
        pk_cols = table.primary_key if table else []
        renames = {
            source: target
            for source, target in (field_mappings or {}).items()
            if source != target
        }

        parts: list[str] = [
            "arangoimport",
            *self._server_flags_for_script(),
            "--file", shlex.quote(self._csv_file_path(table_name)),
            "--type", "csv",
            "--collection", shlex.quote(target_collection),
            "--create-collection", "true",
            "--create-collection-type", "document",
            "--on-duplicate", shlex.quote(self.on_duplicate),
        ]

        # PK → _key: force string so auto-detection doesn't treat numeric IDs as
        # numbers. When a single PK is also mapped to a regular target property,
        # merge it into _key instead of translating it away; arangoimport applies
        # the merge before header translations and therefore preserves both.
        if len(pk_cols) == 1:
            if pk_cols[0] in renames:
                parts.extend([
                    "--merge-attributes",
                    shlex.quote(f"_key=[{pk_cols[0]}]"),
                ])
            else:
                parts.extend(["--translate", shlex.quote(f"{pk_cols[0]}=_key")])
            parts.extend(["--datatype", shlex.quote(f"{pk_cols[0]}=string")])
        elif len(pk_cols) > 1:
            merge_expr = "[" + "]_[".join(pk_cols) + "]"
            parts.extend([
                "--merge-attributes", shlex.quote(f"_key={merge_expr}"),
            ])
            for pk in pk_cols:
                parts.extend(["--datatype", shlex.quote(f"{pk}=string")])

        for source, target in renames.items():
            parts.extend([
                "--translate",
                shlex.quote(f"{source}={target}"),
            ])

        parts.extend(self._datatype_flags(table_name, exclude_cols=set(pk_cols)))
        parts.extend(["--overwrite", "true" if overwrite else "false"])
        return " \\\n    ".join(parts)

    def _build_edge_command(
        self,
        table_name: str,
        edge_collection: str,
        from_collection: str,
        to_collection: str,
        from_fields: list[str],
        *,
        overwrite: bool = True,
    ) -> str:
        """Build arangoimport command for an edge collection from a CSV dump.

        Uses the source table's PK as _from and the FK column(s) as _to,
        with collection prefixes. All non-structural columns are removed
        so the edge is a clean relationship.

        For composite FK edges, ``--merge-attributes`` is used to construct
        ``_to`` from multiple columns.
        """
        table = self.schema.tables.get(table_name)
        pk_cols = table.primary_key if table else []
        all_cols = [c.name for c in table.columns] if table else []

        parts: list[str] = [
            "arangoimport",
            *self._server_flags_for_script(),
            "--file", shlex.quote(self._csv_file_path(table_name)),
            "--type", "csv",
            "--collection", shlex.quote(edge_collection),
            "--create-collection", "true",
            "--create-collection-type", "edge",
            "--on-duplicate", shlex.quote(self.on_duplicate),
        ]

        # PK → _from: force string so _id references resolve correctly
        if len(pk_cols) == 1:
            parts.extend(["--translate", shlex.quote(f"{pk_cols[0]}=_from")])
            parts.extend(["--datatype", shlex.quote(f"{pk_cols[0]}=string")])
        elif len(pk_cols) > 1:
            merge_expr = "[" + "]_[".join(pk_cols) + "]"
            parts.extend([
                "--merge-attributes", shlex.quote(f"_from={merge_expr}"),
            ])
            for pk in pk_cols:
                parts.extend(["--datatype", shlex.quote(f"{pk}=string")])

        # FK → _to: single column uses --translate, composite uses --merge-attributes
        if len(from_fields) == 1:
            parts.extend(["--translate", shlex.quote(f"{from_fields[0]}=_to")])
            parts.extend(["--datatype", shlex.quote(f"{from_fields[0]}=string")])
        else:
            merge_expr = "[" + "]_[".join(from_fields) + "]"
            parts.extend([
                "--merge-attributes", shlex.quote(f"_to={merge_expr}"),
            ])
            for ff in from_fields:
                parts.extend(["--datatype", shlex.quote(f"{ff}=string")])

        parts.extend([
            "--from-collection-prefix", shlex.quote(f"{from_collection}/"),
            "--to-collection-prefix", shlex.quote(f"{to_collection}/"),
        ])

        keep: set[str] = {"_from", "_to"}
        keep.update(from_fields)
        if len(pk_cols) == 1:
            keep.add(pk_cols[0])
        for col_name in all_cols:
            if col_name not in keep and col_name not in pk_cols:
                parts.extend(["--remove-attribute", shlex.quote(col_name)])

        parts.extend(["--overwrite", "true" if overwrite else "false"])
        return " \\\n    ".join(parts)

    def build_document_specs(
        self,
        *,
        overwrite: bool = True,
    ) -> tuple[ImportCommandSpec, ...]:
        """Return direct-CSV document commands with stable identities."""
        specs: list[ImportCommandSpec] = []
        for key, mapping in self.config.collections.items():
            if mapping.collection_type != "document":
                continue
            specs.append(
                ImportCommandSpec(
                    mapping_id=key,
                    kind="document",
                    source_table=mapping.source_table,
                    target_collection=mapping.target_collection,
                    command=self._build_doc_command(
                        mapping.source_table,
                        mapping.target_collection,
                        field_mappings=mapping.field_mappings,
                        overwrite=overwrite,
                    ),
                )
            )
        return tuple(specs)

    def build_edge_specs(
        self,
        *,
        overwrite: bool = True,
    ) -> tuple[ImportCommandSpec, ...]:
        """Return direct-CSV edge commands with stable mapping metadata."""
        specs: list[ImportCommandSpec] = []
        for edge in self.config.edges:
            source_table = _source_table_for_ref(
                self.config,
                edge.from_collection
            )
            from_collection = _target_collection_for_ref(
                self.config,
                edge.from_collection
            )
            to_collection = _target_collection_for_ref(
                self.config,
                edge.to_collection
            )
            specs.append(
                ImportCommandSpec(
                    mapping_id=edge.edge_collection,
                    kind="edge",
                    source_table=source_table,
                    target_collection=edge.edge_collection,
                    edge_collection=edge.edge_collection,
                    from_collection=from_collection,
                    to_collection=to_collection,
                    from_fields=tuple(edge.from_fields),
                    to_fields=tuple(edge.to_fields),
                    command=self._build_edge_command(
                        table_name=source_table,
                        edge_collection=edge.edge_collection,
                        from_collection=from_collection,
                        to_collection=to_collection,
                        from_fields=edge.from_fields,
                        overwrite=overwrite,
                    ),
                )
            )
        return tuple(specs)

    def _fidelity_warnings(self) -> tuple[str, ...]:
        limitations: set[str] = set()
        for mapping in self.config.collections.values():
            if mapping.field_expressions:
                limitations.add("field expressions")
            if mapping.include_fields is not None:
                limitations.add("include-field filters")
            if mapping.exclude_fields:
                limitations.add("exclude-field filters")
        if self.config.type_overrides:
            limitations.add("mapping-level type overrides")
        if not limitations:
            return ()
        return (
            "CSV-direct import cannot reproduce "
            + ", ".join(sorted(limitations))
            + "; use JSONL mode for mapping-faithful output.",
        )

    def _build_graph_creation_arangosh(self, graph_name: str) -> list[str]:
        """Build arangosh command to create a named graph."""
        return _build_graph_creation_arangosh(self.config, graph_name)

    def render_script(
        self,
        *,
        overwrite_on_initial: bool = True,
        graph_name: str | None = None,
        secret_safe: bool = False,
        generated_at: str | None = None,
    ) -> str:
        """Render direct-CSV commands without writing a script."""
        documents = self.build_document_specs(overwrite=overwrite_on_initial)
        edges = self.build_edge_specs(overwrite=overwrite_on_initial)
        return self._render_script(
            documents,
            edges,
            graph_name=graph_name,
            secret_safe=secret_safe,
            generated_at=generated_at,
        )

    def _render_script(
        self,
        documents: tuple[ImportCommandSpec, ...],
        edges: tuple[ImportCommandSpec, ...],
        *,
        graph_name: str | None,
        secret_safe: bool,
        generated_at: str | None = None,
    ) -> str:
        """Render a CSV script from already-built command specifications."""
        generated_at = generated_at or datetime.now(timezone.utc).isoformat()
        lines: list[str] = [
            "#!/usr/bin/env bash",
            "# Generated by R2G CsvImportGenerator",
            f"# {generated_at}",
            "# Imports PG CSV dumps directly via arangoimport --type csv",
            "# No intermediate JSONL transformation required",
            "set -euo pipefail",
            "",
            *_script_environment(
                endpoint=self.endpoint,
                database=self.database,
                username=self.username,
                password=self.password,
                secret_safe=secret_safe,
            ),
            "",
        ]
        for warning in self._fidelity_warnings():
            lines.append(f"# WARNING: {warning}")
        lines.extend(["", "# === Document collections ===", ""])
        for spec in documents:
            msg = (
                f"Importing document collection {spec.target_collection} "
                f"from {spec.source_table}.csv..."
            )
            lines.append("echo " + shlex.quote(msg))
            lines.append(spec.command)
            lines.append("")

        lines.append("# === Edge collections ===")
        lines.append("")
        for spec in edges:
            lines.append(
                "echo " + shlex.quote(
                    f"Importing edge collection {spec.edge_collection} "
                    f"({spec.from_collection} -> {spec.to_collection}) "
                    f"from {spec.source_table}.csv..."
                )
            )
            lines.append(spec.command)
            lines.append("")
        if graph_name:
            lines.append("# === Named graph ===")
            lines.append("")
            lines.extend(self._build_graph_creation_arangosh(graph_name))
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def build_plan(
        self,
        *,
        graph_name: str = "r2g_graph",
        overwrite_on_initial: bool = True,
        secret_safe: bool = True,
    ) -> ImportPlan:
        """Build a structured direct-CSV artifact for CLI/UI consumers."""
        if self.config.graph_layout == "lpg":
            raise ValueError(
                "arangoimport preview does not support LPG layout; use the "
                "Studio streaming Load path for this mapping"
            )
        documents = self.build_document_specs(overwrite=overwrite_on_initial)
        edges = self.build_edge_specs(overwrite=overwrite_on_initial)
        return ImportPlan(
            mode="csv",
            data_dir=self.data_dir,
            script=self._render_script(
                documents,
                edges,
                graph_name=graph_name,
                secret_safe=secret_safe,
            ),
            documents=documents,
            edges=edges,
            graph_script=_render_graph_shell(
                self.config,
                graph_name,
                endpoint=self.endpoint,
                database=self.database,
                username=self.username,
                password=self.password,
                secret_safe=secret_safe,
            ),
            warnings=self._fidelity_warnings(),
        )

    def generate_csv_script(
        self,
        output_path: str,
        overwrite_on_initial: bool = True,
        graph_name: str | None = None,
    ) -> str:
        """Render, write, and make executable a direct-CSV import script."""
        content = self.render_script(
            overwrite_on_initial=overwrite_on_initial,
            graph_name=graph_name,
            secret_safe=False,
        )
        out = Path(output_path)
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(content, encoding="utf-8")
            os.chmod(out, 0o755)
        except OSError as e:
            logger.exception(
                "failed_to_write_csv_import_script",
                output_path=str(out),
                error=str(e),
            )
            raise
        return content
