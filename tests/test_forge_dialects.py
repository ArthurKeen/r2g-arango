"""Unit tests for the Federation Forge dialect seam (``r2g.forge.dialects``).

No live database here: these pin the *shape* of each dialect's DDL and loader,
the registry contract every dialect must satisfy, and ADR-0006 D-2 — data is
synthesized once and only projected per system, so ``rows`` are byte-identical
across dialects. The live roundtrips live in ``tests/integration/``.
"""

from __future__ import annotations

import json

import pytest
from test_forge import sample_conceptual, sample_ontology

from r2g.config import pg_type_to_json_type
from r2g.connectors.clickhouse import _clean_type
from r2g.forge import (
    DIALECTS,
    JSON_TYPES,
    SUPPORTED_DIALECTS,
    ForgeError,
    ForgeOntology,
    generate,
    get_dialect,
    plan_schema,
    split_sql_statements,
    synthesize_rows,
)
from r2g.forge.core import (
    ROLE_FOREIGN_KEY,
    ROLE_PRIMARY_KEY,
    ColumnPlan,
    SchemaPlan,
    TablePlan,
)
from r2g.forge.dialects.arango import (
    GRAPH_NAME,
    MANIFEST_VERSION,
    project_documents,
    render_manifest,
)
from r2g.forge.dialects.base import check_type_table, sql_literal
from r2g.forge.dialects.clickhouse import CLICKHOUSE_TYPE_FOR_JSON_TYPE, fk_intent_comment
from r2g.forge.dialects.snowflake import SNOWFLAKE_TYPE_FOR_JSON_TYPE

SQL_DIALECTS = ("postgres", "snowflake", "clickhouse")


class TestRegistry:
    def test_launch_set_registered_postgres_first(self):
        # ADR-0006 D-4 launch set; postgres stays first (the CLI default).
        assert SUPPORTED_DIALECTS == ("postgres", "snowflake", "clickhouse", "arango")
        assert set(DIALECTS) == set(SUPPORTED_DIALECTS)

    @pytest.mark.parametrize("name", SUPPORTED_DIALECTS)
    def test_every_dialect_covers_every_json_type(self, name):
        dialect = get_dialect(name)
        assert dialect.name == name
        assert set(dialect.type_for_json) >= set(JSON_TYPES)
        check_type_table(dialect)  # does not raise

    def test_check_type_table_refuses_incomplete_dialect(self):
        class Half(type(get_dialect("postgres"))):  # type: ignore[misc]
            name = "half"
            type_for_json = {"integer": "bigint"}

        with pytest.raises(ForgeError, match="no physical type"):
            check_type_table(Half())

    def test_unknown_dialect_lists_supported(self):
        with pytest.raises(ForgeError, match="duckdb.*postgres"):
            get_dialect("duckdb")

    def test_generate_stamps_registry_name(self):
        for name in SUPPORTED_DIALECTS:
            assert generate(sample_ontology(), dialect=name, seed=1).dialect == name


class TestRowsAreDialectIndependent:
    """ADR-0006 D-2: synthesized once, projected per system."""

    @pytest.mark.parametrize("seed", [0, 7, 421])
    def test_rows_byte_identical_across_all_dialects(self, seed):
        rendered = {
            name: json.dumps(
                generate(sample_ontology(), dialect=name, seed=seed, rows_per_entity=13).rows,
                sort_keys=True,
            )
            for name in SUPPORTED_DIALECTS
        }
        assert len(set(rendered.values())) == 1, "rows diverge across dialects"

    def test_rows_keyed_by_canonical_names_not_physical_spelling(self):
        snowflake = generate(sample_ontology(), dialect="snowflake", seed=1)
        assert set(snowflake.rows) == {"accounts", "contacts", "tickets"}
        assert "account_name" in snowflake.rows["accounts"][0]
        # ...while the loader speaks the dialect's spelling.
        assert "INSERT INTO ACCOUNTS (ID, ACCOUNT_NAME" in snowflake.load_sql

    def test_ddl_is_seed_independent_per_dialect(self):
        for name in SUPPORTED_DIALECTS:
            a = generate(sample_ontology(), dialect=name, seed=1)
            b = generate(sample_ontology(), dialect=name, seed=2)
            assert a.ddl == b.ddl, name
            assert a.rows != b.rows, name

    def test_rerun_is_byte_identical_per_dialect(self):
        for name in SUPPORTED_DIALECTS:
            a = generate(sample_ontology(), dialect=name, seed=99)
            b = generate(sample_ontology(), dialect=name, seed=99)
            assert (a.ddl, a.load_sql, a.rows) == (b.ddl, b.load_sql, b.rows), name


class TestSchemaPlan:
    def test_plan_orders_parents_first_and_columns_by_role(self):
        plan = plan_schema(sample_ontology())
        assert [t.table for t in plan.tables] == ["accounts", "contacts", "tickets"]
        contacts = plan.table("contacts")
        assert [c.name for c in contacts.columns] == ["id", "account_id", "full_name", "is_primary"]
        assert contacts.primary_key.role == "pk" and not contacts.primary_key.nullable
        (fk,) = contacts.foreign_keys
        assert fk.references == "accounts" and not fk.nullable
        assert all(c.nullable for c in contacts.properties)

    def test_plan_edges_carry_forward_pipeline_names(self):
        plan = plan_schema(sample_ontology())
        assert [(e.edge_collection, e.relationship) for e in plan.edges] == [
            ("contacts_to_accounts", "contactsToAccounts"),
            ("tickets_to_contacts", "ticketsToContacts"),
        ]

    def test_fk_carries_the_parent_key_not_the_child_key(self):
        """``references_column`` names the column in the PARENT table.

        Regression: every dialect rendered ``table.primary_key`` for the
        referenced column — this table's key, not the parent's. Correct only
        while every table shares SURROGATE_KEY, which is why it went unseen.
        """
        contacts = plan_schema(sample_ontology()).table("contacts")
        (fk,) = contacts.foreign_keys
        assert fk.references == "accounts"
        assert fk.references_column == "id"
        for edge in plan_schema(sample_ontology()).edges:
            assert edge.to_key == "id"

    def test_plan_is_a_pure_function_of_the_ontology(self):
        assert plan_schema(sample_ontology()) == plan_schema(sample_ontology())

    def test_unknown_table_lookup_is_forge_error(self):
        with pytest.raises(ForgeError, match="unknown planned table"):
            plan_schema(sample_ontology()).table("ghosts")


class TestForeignKeyReferencesTheParent:
    """The referenced column must come from the parent table.

    ``plan_schema`` gives every table the same surrogate key, so a real plan
    cannot distinguish "parent's key" from "child's key". These build a plan
    where they differ, which is the only way to see the bug.
    """

    @staticmethod
    def _plan_with_divergent_keys() -> SchemaPlan:
        parent = TablePlan(
            entity="Account",
            table="accounts",
            columns=(ColumnPlan(name="account_pk", json_type="integer", role=ROLE_PRIMARY_KEY),),
        )
        child = TablePlan(
            entity="Contact",
            table="contacts",
            columns=(
                ColumnPlan(name="contact_pk", json_type="integer", role=ROLE_PRIMARY_KEY),
                ColumnPlan(
                    name="account_id",
                    json_type="integer",
                    role=ROLE_FOREIGN_KEY,
                    references="accounts",
                    references_column="account_pk",
                ),
            ),
        )
        return SchemaPlan(tables=(parent, child), edges=())

    @pytest.mark.parametrize("name", ["postgres", "snowflake"])
    def test_sql_dialects_reference_the_parents_key(self, name):
        ddl = get_dialect(name).render_ddl(self._plan_with_divergent_keys())
        fk_line = next(line for line in ddl.splitlines() if "FOREIGN KEY" in line)
        assert "accounts" in fk_line.lower()
        # the parent's key, not the child's
        assert "account_pk" in fk_line.lower()
        assert "contact_pk" not in fk_line.lower()

    def test_clickhouse_fk_comment_names_the_parents_key(self):
        ddl = get_dialect("clickhouse").render_ddl(self._plan_with_divergent_keys())
        comment = next(line for line in ddl.splitlines() if "forge:foreign-key" in line)
        assert "accounts(account_pk)" in comment
        assert "contact_pk" not in comment


class TestSqlHelpers:
    def test_sql_literal_renders_each_type(self):
        assert sql_literal(None) == "NULL"
        assert sql_literal(True) == "TRUE" and sql_literal(False) == "FALSE"
        assert sql_literal(True, true="true", false="false") == "true"
        assert sql_literal(42) == "42" and sql_literal(1.5) == "1.5"
        assert sql_literal("it's") == "'it''s'"

    def test_split_sql_statements_skips_comments_and_respects_strings(self):
        sql = "-- header\nINSERT INTO t (s) VALUES ('a;b');\n\n-- more\nCREATE TABLE x (id int)"
        assert split_sql_statements(sql) == [
            "INSERT INTO t (s) VALUES ('a;b')",
            "CREATE TABLE x (id int)",
        ]

    @pytest.mark.parametrize(
        "label,sql",
        [
            ("trailing comment hiding a semicolon", "SELECT 1 -- note; more\nFROM t;\nSELECT 2;"),
            ("apostrophe inside a quoted identifier", 'CREATE TABLE "O\'Brien" (a INT);\nSELECT 1;'),
            ("block comment containing a semicolon", "SELECT 1 /* hi; there */ FROM t;\nSELECT 2;"),
            ("'' escape inside a literal", "INSERT INTO t VALUES ('it''s; fine');\nSELECT 2;"),
            ("semicolon inside a plain literal", "INSERT INTO t VALUES ('a;b');\nSELECT 2;"),
            ("no trailing semicolon", "SELECT 1;\nSELECT 2"),
            ("dollar-quoted body", "CREATE FUNCTION f() AS $$ SELECT 1; $$ LANGUAGE sql;\nSELECT 2;"),
            ("backtick identifier", "CREATE TABLE `a;b` (x Int64);\nSELECT 2;"),
            ("backslash is literal in standard SQL", "INSERT INTO t VALUES ('C:\\');\nSELECT 2;"),
        ],
    )
    def test_split_survives_sql_it_did_not_generate(self, label, sql):
        """This helper is public and gets pointed at hand-written scripts, not
        only at the forge's own output, so a ``;`` must split only when it is
        really a separator.

        Regression: ``--`` was recognised only at the START of a line, ``"``
        quoted identifiers were not tracked at all, and block comments were not
        understood. Each case below previously produced a bogus statement, or a
        corrupted one after it.
        """
        assert len(split_sql_statements(sql)) == 2, label

    def test_backslash_escapes_are_opt_in_for_clickhouse_and_mysql(self):
        """Standard SQL reads a backslash literally; ClickHouse and MySQL treat
        it as an escape. Guessing would corrupt whichever engine guessed wrong,
        so the caller names which rules apply."""
        sql = "INSERT INTO t VALUES ('it\\'s; x');\nSELECT 2;"
        assert len(split_sql_statements(sql, backslash_escapes=True)) == 2

    @pytest.mark.parametrize(
        "label,sql",
        [
            ("unterminated block comment", "SELECT 1; /* oops\nSELECT 2;"),
            ("unterminated literal", "SELECT 'oops;\nSELECT 2;"),
            ("unterminated identifier", 'SELECT "oops;\nSELECT 2;'),
        ],
    )
    def test_unterminated_quoting_raises_rather_than_truncating(self, label, sql):
        """Regression: these ran to end-of-input and silently dropped every
        remaining statement, so a stray `/*` meant the tail of a script was
        never executed and nothing said so."""
        with pytest.raises(ForgeError, match="unterminated"):
            split_sql_statements(sql)

    @pytest.mark.parametrize("name", SQL_DIALECTS)
    def test_generated_scripts_split_into_one_statement_per_table_or_row(self, name):
        artifacts = generate(sample_ontology(), dialect=name, seed=1, rows_per_entity=4)
        assert len(split_sql_statements(artifacts.ddl)) == 3
        loader_statements = split_sql_statements(artifacts.load_sql)
        expected = 3 * 4 if name == "postgres" else 3
        assert len(loader_statements) == expected
        assert all(s.startswith("INSERT INTO") for s in loader_statements)


class TestSnowflakeDialect:
    def test_types_roundtrip_through_the_forward_map_except_the_known_number_gap(self):
        # INFORMATION_SCHEMA reports NUMBER(38,0) as bare "number", which the
        # forward map calls float — the one documented fidelity gap
        # (tests/integration/test_forge_roundtrip_snowflake.py pins it).
        introspects_as = {"integer": "number", "float": "float", "boolean": "boolean", "string": "text"}
        for json_type, physical in SNOWFLAKE_TYPE_FOR_JSON_TYPE.items():
            assert physical.split("(")[0].lower() in {introspects_as[json_type], "varchar"}
        assert pg_type_to_json_type("float") == "float"
        assert pg_type_to_json_type("boolean") == "boolean"
        assert pg_type_to_json_type("text") == "string"
        assert pg_type_to_json_type("number") == "float"  # the gap, pinned

    def test_ddl_uses_uppercase_unquoted_identifiers_and_declared_keys(self):
        ddl = generate(sample_ontology(), dialect="snowflake", seed=1).ddl
        assert "CREATE TABLE ACCOUNTS (" in ddl
        assert "    ID NUMBER(38,0) NOT NULL," in ddl
        assert "    ACCOUNT_NAME VARCHAR," in ddl
        assert "    HEALTH_SCORE FLOAT," in ddl
        assert "    IS_PRIMARY BOOLEAN," in ddl
        assert "PRIMARY KEY (ID)" in ddl
        assert "FOREIGN KEY (ACCOUNT_ID) REFERENCES ACCOUNTS (ID)" in ddl
        assert '"' not in ddl, "identifiers are unquoted so folding applies"
        assert "not enforced" in ddl

    def test_ddl_orders_parents_before_children(self):
        ddl = generate(sample_ontology(), dialect="snowflake", seed=1).ddl
        accounts, contacts, tickets = (ddl.index(f"CREATE TABLE {t}") for t in ("ACCOUNTS", "CONTACTS", "TICKETS"))
        assert accounts < contacts < tickets

    def test_loader_is_one_multirow_insert_per_table(self):
        load = generate(sample_ontology(), dialect="snowflake", seed=1, rows_per_entity=5).load_sql
        assert load.count("INSERT INTO") == 3
        assert "INSERT INTO CONTACTS (ID, ACCOUNT_ID, FULL_NAME, IS_PRIMARY) VALUES\n" in load
        assert load.count("\n    (") == 15
        assert "TRUE" in load or "FALSE" in load


class TestClickHouseDialect:
    def test_types_roundtrip_through_the_real_connector_and_forward_map(self):
        # ClickHouseConnector._clean_type lowercases the system.columns type;
        # pg_type_to_json_type must land back on the declared JSON type.
        for json_type, physical in CLICKHOUSE_TYPE_FOR_JSON_TYPE.items():
            assert pg_type_to_json_type(_clean_type(physical)) == json_type
            assert pg_type_to_json_type(_clean_type(f"Nullable({physical})")) == json_type

    def test_ddl_is_mergetree_ordered_by_spine_with_nullable_properties(self):
        ddl = generate(sample_ontology(), dialect="clickhouse", seed=1).ddl
        assert "CREATE TABLE contacts (\n    id Int64,\n" in ddl
        assert (
            "    full_name Nullable(String),\n    is_primary Nullable(Bool)\n) ENGINE = MergeTree ORDER BY (id);"
        ) in ddl
        assert "PRIMARY KEY" not in ddl and "FOREIGN KEY (" not in ddl

    def test_fk_intent_recorded_as_column_comment_and_header(self):
        ddl = generate(sample_ontology(), dialect="clickhouse", seed=1).ddl
        assert f"account_id Int64 COMMENT '{fk_intent_comment('accounts', 'id')}'" in ddl
        assert "-- FOREIGN KEY intent: contacts.account_id -> accounts(id)  [contactsToAccounts]" in ddl
        load = generate(sample_ontology(), dialect="clickhouse", seed=1).load_sql
        lines = load.splitlines()
        assert lines[0].startswith("-- Federation Forge") and lines[1].startswith("-- dialect: clickhouse")
        assert lines[2] == "-- FOREIGN KEY intent (not enforceable in ClickHouse): contacts.account_id -> accounts(id)"

    def test_ddl_routes_every_name_through_the_seam(self):
        """A dialect that folds identifiers must fold them in the DDL too.

        Regression: ``column_ddl``, ``table_suffix`` and both FK-intent header
        lines emitted canonical names directly while ``render_loader`` always
        projected them — so a folding subclass produced DDL its own loader
        could not target. Inert for ClickHouse (identity mapping), which is
        exactly why a subclass is needed to see it.
        """

        class Folding(type(get_dialect("clickhouse"))):
            name = "clickhouse_folding"

            def physical_table(self, table: str) -> str:
                return table.upper()

            def physical_column(self, column: str) -> str:
                return column.upper()

        ddl = Folding().render_ddl(plan_schema(sample_ontology()))
        assert "ACCOUNT_ID" in ddl, "column_ddl did not project the column name"
        assert "ORDER BY (ID)" in ddl, "table_suffix did not project the key"
        assert "CONTACTS.ACCOUNT_ID -> ACCOUNTS(ID)" in ddl, "FK header did not project"
        # the canonical spellings must not survive alongside the folded ones
        assert "account_id Int64" not in ddl and "ORDER BY (id)" not in ddl

    def test_loader_uses_lowercase_boolean_literals(self):
        load = generate(sample_ontology(), dialect="clickhouse", seed=3).load_sql
        assert "TRUE" not in load and "FALSE" not in load
        assert " true" in load or " false" in load
        assert load.count("INSERT INTO") == 3


class TestArangoDialect:
    def test_loader_recreates_the_graph_rather_than_skipping_it(self):
        """The loader calls itself idempotent, so a re-run must not inherit
        the previous ontology's edge definitions.

        Regression: it skipped ``create_graph`` whenever a graph of that name
        existed. GRAPH_NAME is a constant and ARANGO_DB defaults to _system, so
        a second ontology imported its collections, kept the OLD edge
        definitions, and exited 0 — the analyzer then read stale relationships.
        """
        loader = generate(sample_ontology(), dialect="arango", seed=1).load_sql
        assert "delete_graph" in loader, "an existing graph is never torn down"
        assert "drop_collections=False" in loader, "tear-down must keep the documents"
        assert "not db.has_graph" not in loader, "the skip-if-exists branch is back"

    def test_manifest_shape(self):
        artifacts = generate(sample_ontology(), dialect="arango", seed=1)
        manifest = json.loads(artifacts.ddl)
        assert manifest["forgeManifestVersion"] == MANIFEST_VERSION
        assert manifest["keyField"] == "id"
        assert [c["name"] for c in manifest["collections"]] == ["accounts", "contacts", "tickets"]
        assert {c["entity"] for c in manifest["collections"]} == {"Account", "Contact", "Ticket"}
        edges = {e["name"]: e for e in manifest["edgeCollections"]}
        assert set(edges) == {"contacts_to_accounts", "tickets_to_contacts"}
        assert edges["contacts_to_accounts"]["from"] == {"collection": "contacts", "field": "id"}
        assert edges["contacts_to_accounts"]["to"] == {"collection": "accounts", "field": "account_id"}
        assert edges["contacts_to_accounts"]["relationship"] == "contactsToAccounts"
        assert manifest["graph"]["name"] == GRAPH_NAME
        assert manifest["graph"]["edgeDefinitions"][0] == {
            "collection": "contacts_to_accounts",
            "from": ["contacts"],
            "to": ["accounts"],
        }

    def test_manifest_matches_render_manifest_and_is_sorted(self):
        artifacts = generate(sample_ontology(), dialect="arango", seed=1)
        assert json.loads(artifacts.ddl) == render_manifest(plan_schema(sample_ontology()))
        assert artifacts.ddl == json.dumps(json.loads(artifacts.ddl), indent=2, sort_keys=True) + "\n"

    def test_project_documents_keys_and_edges_on_the_spine(self):
        artifacts = generate(sample_ontology(), dialect="arango", seed=7, rows_per_entity=6)
        docs = project_documents(json.loads(artifacts.ddl), artifacts.rows)
        assert set(docs) == {"accounts", "contacts", "tickets", "contacts_to_accounts", "tickets_to_contacts"}
        for row, doc in zip(artifacts.rows["contacts"], docs["contacts"]):
            assert doc["_key"] == str(row["id"])
            assert {k: v for k, v in doc.items() if k != "_key"} == row
        account_keys = {d["_key"] for d in docs["accounts"]}
        for edge, row in zip(docs["contacts_to_accounts"], artifacts.rows["contacts"]):
            assert set(edge) == {"_key", "_from", "_to"}, "edges carry no relationship properties"
            assert edge["_from"] == f"contacts/{row['id']}"
            assert edge["_to"] == f"accounts/{row['account_id']}"
            assert edge["_to"].split("/")[1] in account_keys

    def test_rows_are_untouched_by_projection(self):
        artifacts = generate(sample_ontology(), dialect="arango", seed=7)
        before = json.dumps(artifacts.rows, sort_keys=True)
        project_documents(json.loads(artifacts.ddl), artifacts.rows)
        assert json.dumps(artifacts.rows, sort_keys=True) == before
        assert "_key" not in artifacts.rows["accounts"][0]

    def test_loader_script_is_valid_standalone_python_with_seed_stamp(self):
        artifacts = generate(sample_ontology(), dialect="arango", seed=421)
        compile(artifacts.load_sql, "forge.load.py", "exec")
        assert "seed: 421" in artifacts.load_sql
        assert "import r2g" not in artifacts.load_sql, "the loader must not depend on r2g"
        assert "from arango import ArangoClient" in artifacts.load_sql
        assert "on_duplicate=\"replace\"" in artifacts.load_sql

    def test_loader_script_projection_agrees_with_library(self):
        # The script embeds its own project_documents; execute it in isolation
        # and compare with the library function so the two cannot drift.
        artifacts = generate(sample_ontology(), dialect="arango", seed=5, rows_per_entity=4)
        namespace: dict = {"__name__": "forge_loader_under_test"}
        exec(compile(artifacts.load_sql, "forge.load.py", "exec"), namespace)
        manifest = json.loads(artifacts.ddl)
        assert namespace["project_documents"](manifest, artifacts.rows) == project_documents(manifest, artifacts.rows)

    def test_write_to_uses_arango_file_names(self, tmp_path):
        artifacts = generate(sample_ontology(), dialect="arango", seed=5)
        names = sorted(p.rsplit("/", 1)[-1] for p in artifacts.write_to(str(tmp_path)))
        assert names == ["forge.collections.json", "forge.load.py", "forge.rows.json"]
        assert json.loads((tmp_path / "forge.collections.json").read_text())["dialect"] == "arango"

    @pytest.mark.parametrize("name", SQL_DIALECTS)
    def test_write_to_keeps_sql_file_names_for_sql_dialects(self, name, tmp_path):
        paths = generate(sample_ontology(), dialect=name, seed=5).write_to(str(tmp_path))
        names = sorted(p.rsplit("/", 1)[-1] for p in paths)
        assert names == ["forge.load.sql", "forge.rows.json", "forge.sql"]


def test_sample_conceptual_still_refused_when_type_missing_for_any_dialect():
    doc = sample_conceptual()
    doc["entities"][0]["properties"][0]["type"] = "temporal"
    for name in SUPPORTED_DIALECTS:
        with pytest.raises(ForgeError, match="unsupported type"):
            generate(ForgeOntology.from_conceptual(doc), dialect=name, seed=1)


class TestDivergentKeysRenderEndToEnd:
    """A plan whose key is not named `id` must render AND load.

    The parent-key regression test asserted only on ``render_ddl``, so it passed
    while ``synthesize_rows`` still stamped every row with the SURROGATE_KEY
    literal — each dialect emitted DDL its own loader could not populate, and
    the one test that existed to defend the divergent case never called the
    loader that would have caught it.
    """

    @pytest.mark.parametrize("name", ["postgres", "snowflake", "clickhouse"])
    def test_loader_populates_the_columns_the_ddl_declares(self, name):
        plan = TestForeignKeyReferencesTheParent._plan_with_divergent_keys()
        rows = synthesize_rows(plan, seed=1, rows_per_entity=3)
        assert sorted(rows["accounts"][0]) == ["account_pk"]
        loader = get_dialect(name).render_loader(plan, rows, 1)   # must not KeyError
        assert "account_pk" in loader.lower()
