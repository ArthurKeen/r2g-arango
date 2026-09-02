"""LPG target layout (P13): one node collection + one edge collection.

The type stops being the collection and becomes data — ``labels`` on nodes,
``type`` on edges — plus copies of each edge's endpoint labels, which is what
makes a vertex-centric index able to filter a traversal from the index alone.

Two collapse hazards get explicit coverage here, because both lose data
*silently* rather than erroring: node ``_key`` collisions across former tables,
and edge ``_key`` collisions between two edge types joining the same pair.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from r2g.config import ConfigManager
from r2g.connectors.arango_writer import ArangoWriter
from r2g.csi import mapping_to_csi, validate_csi
from r2g.lpg import (
    EDGE_INBOUND_INDEX,
    EDGE_OUTBOUND_INDEX,
    edge_indexes,
    graph_edge_definition,
    label_predicate,
    traversal_index_hint,
    traversal_templates,
    uses_vertex_centric_index,
    vertex_indexes,
)
from r2g.streaming.pipeline import StreamingPipeline
from r2g.transformers.edge_transformer import EdgeTransformer
from r2g.transformers.node_transformer import NodeTransformer
from r2g.types import (
    CollectionMapping,
    Column,
    EdgeDefinition,
    LpgLayout,
    MappingConfig,
    Schema,
    Table,
)

LPG = LpgLayout()


def _accounts() -> Table:
    return Table(
        name="accounts",
        columns=[
            Column(name="id", data_type="integer", is_primary_key=True),
            Column(name="name", data_type="text"),
        ],
        primary_key=["id"],
    )


def _orders() -> Table:
    return Table(
        name="orders",
        columns=[
            Column(name="id", data_type="integer", is_primary_key=True),
            Column(name="account_id", data_type="integer"),
        ],
        primary_key=["id"],
    )


# ── nodes ────────────────────────────────────────────────────────────


class TestLpgNodes:
    def test_label_and_origin_are_carried_on_the_document(self):
        nt = NodeTransformer(
            _accounts(),
            CollectionMapping(source_table="accounts", target_collection="Account"),
            lpg=LPG,
        )
        doc = nt.transform_row({"id": 42, "name": "Acme"})
        assert doc["labels"] == ["Account"]
        assert doc["sourceTable"] == "accounts"
        assert doc["name"] == "Acme"

    def test_key_is_label_namespaced(self):
        nt = NodeTransformer(
            _accounts(),
            CollectionMapping(source_table="accounts", target_collection="Account"),
            lpg=LPG,
        )
        assert nt.transform_row({"id": 42, "name": "Acme"})["_key"] == "Account_42"

    def test_same_pk_in_two_tables_does_not_collide(self):
        """The silent-overwrite hazard: pg keeps these in separate collections."""
        acc = NodeTransformer(
            _accounts(),
            CollectionMapping(source_table="accounts", target_collection="Account"),
            lpg=LPG,
        ).transform_row({"id": 1, "name": "Acme"})
        order = NodeTransformer(
            _orders(),
            CollectionMapping(source_table="orders", target_collection="Order"),
            lpg=LPG,
        ).transform_row({"id": 1, "account_id": 1})
        assert acc["_key"] != order["_key"]
        assert (acc["_key"], order["_key"]) == ("Account_1", "Order_1")

    def test_pg_layout_is_untouched(self):
        doc = NodeTransformer(
            _accounts(),
            CollectionMapping(source_table="accounts", target_collection="Account"),
        ).transform_row({"id": 42, "name": "Acme"})
        assert doc["_key"] == "42"
        assert "labels" not in doc and "sourceTable" not in doc


# ── edges ────────────────────────────────────────────────────────────


def _edge_tx(edge_collection: str = "placedBy", **kw) -> EdgeTransformer:
    return EdgeTransformer(
        EdgeDefinition(
            edge_collection=edge_collection,
            from_collection="orders",
            to_collection="accounts",
            from_fields=["account_id"],
            to_fields=["id"],
        ),
        _orders(),
        from_name="Order",
        to_name="Account",
        **kw,
    )


class TestLpgEdges:
    def test_type_and_endpoint_labels_are_copied_onto_the_edge(self):
        doc = _edge_tx(lpg=LPG).transform_row({"id": 7, "account_id": 42})
        assert doc["type"] == "placedBy"
        # The copies a vertex-centric index filters on.
        assert doc["fromLabels"] == ["Order"]
        assert doc["toLabels"] == ["Account"]

    def test_endpoints_target_the_single_node_collection(self):
        doc = _edge_tx(lpg=LPG).transform_row({"id": 7, "account_id": 42})
        assert doc["_from"] == "nodes/Order_7"
        assert doc["_to"] == "nodes/Account_42"

    def test_two_edge_types_between_the_same_pair_do_not_collide(self):
        """pg gives these separate edge collections; lpg must key them apart."""
        a = _edge_tx("placedBy", lpg=LPG).transform_row({"id": 7, "account_id": 42})
        b = _edge_tx("billedTo", lpg=LPG).transform_row({"id": 7, "account_id": 42})
        assert a["_key"] != b["_key"]
        assert a["_key"].startswith("placedBy") and b["_key"].startswith("billedTo")

    def test_pg_layout_is_untouched(self):
        doc = _edge_tx().transform_row({"id": 7, "account_id": 42})
        assert doc["_from"] == "Order/7"
        assert doc["_to"] == "Account/42"
        assert doc["_key"] == "7_42"
        assert "type" not in doc and "fromLabels" not in doc


# ── config ───────────────────────────────────────────────────────────


class TestLpgConfig:
    def test_default_config_omits_the_layout_entirely(self):
        """Byte-stability: a pre-P13 mapping must serialize unchanged."""
        dumped = MappingConfig(source_schema="public").model_dump()
        assert "graph_layout" not in dumped
        assert "lpg" not in dumped

    def test_opting_in_persists_the_layout(self):
        dumped = MappingConfig(source_schema="public", graph_layout="lpg").model_dump()
        assert dumped["graph_layout"] == "lpg"
        assert dumped["lpg"]["node_collection"] == "nodes"
        assert dumped["lpg"]["edge_collection"] == "edges"

    def test_layout_round_trips(self):
        cfg = MappingConfig(
            source_schema="public",
            graph_layout="lpg",
            lpg=LpgLayout(node_collection="v", edge_collection="e"),
        )
        back = MappingConfig.model_validate(cfg.model_dump())
        assert back.graph_layout == "lpg"
        assert (back.lpg.node_collection, back.lpg.edge_collection) == ("v", "e")


# ── indexes / graph definition / templates ───────────────────────────


class TestLpgIndexes:
    def test_vertex_index_expands_the_label_array(self):
        (idx,) = vertex_indexes(LPG)
        assert idx["fields"] == ["labels[*]"]
        assert idx["type"] == "persistent"

    def test_vertex_centric_indexes_lead_with_the_traversal_endpoint(self):
        out, inb = edge_indexes(LPG)
        # _from/_to must lead: that is the lookup the traversal performs.
        assert out["fields"] == ["_from", "type", "toLabels[*]"]
        assert inb["fields"] == ["_to", "type", "fromLabels[*]"]

    def test_each_index_expands_at_most_one_array(self):
        """ArangoDB permits a single array expansion per persistent index."""
        for idx in (*vertex_indexes(LPG), *edge_indexes(LPG)):
            assert sum(1 for f in idx["fields"] if f.endswith("[*]")) <= 1

    def test_index_names_are_stable_for_idempotent_provisioning(self):
        assert {i["name"] for i in edge_indexes(LPG)} == {
            EDGE_OUTBOUND_INDEX,
            EDGE_INBOUND_INDEX,
        }

    def test_field_names_follow_the_configured_layout(self):
        custom = LpgLayout(type_field="rel", to_labels_field="dstLabels")
        out, _ = edge_indexes(custom)
        assert out["fields"] == ["_from", "rel", "dstLabels[*]"]


class TestLpgGraphDefinition:
    def test_single_edge_definition_over_one_node_collection(self):
        d = graph_edge_definition(LPG)
        assert d["edge_collection"] == "edges"
        assert d["from_vertex_collections"] == ["nodes"]
        assert d["to_vertex_collections"] == ["nodes"]

    def test_config_manager_returns_exactly_one_definition(self):
        cfg = MappingConfig(source_schema="public", graph_layout="lpg")
        cfg.edges = [
            EdgeDefinition(
                edge_collection=n,
                from_collection="orders",
                to_collection="accounts",
                from_fields=["account_id"],
                to_fields=["id"],
            )
            for n in ("placedBy", "billedTo")
        ]
        # Two edge types, but the LPG layout is one edge collection.
        assert len(ConfigManager.graph_edge_definitions(cfg)) == 1

    def test_pg_layout_still_yields_one_definition_per_edge(self):
        cfg = MappingConfig(source_schema="public")
        cfg.collections = {
            "accounts": CollectionMapping(source_table="accounts", target_collection="Account"),
            "orders": CollectionMapping(source_table="orders", target_collection="Order"),
        }
        cfg.edges = [
            EdgeDefinition(
                edge_collection=n,
                from_collection="orders",
                to_collection="accounts",
                from_fields=["account_id"],
                to_fields=["id"],
            )
            for n in ("placedBy", "billedTo")
        ]
        assert len(ConfigManager.graph_edge_definitions(cfg)) == 2


class TestLpgTraversalTemplates:
    def test_filters_match_the_indexed_fields(self):
        t = traversal_templates(LPG)["outbound_by_type"]
        assert "e.type == @type" in t
        assert "@toLabel IN e.toLabels" in t

    def test_label_test_avoids_the_ALL_over_scalar_no_op(self):
        """`arr[*] ALL == x` over a one-element array is trivially true — a
        filter that reads as restrictive and restricts nothing."""
        for q in traversal_templates(LPG).values():
            assert "ALL ==" not in q

    def test_traversals_name_their_vertex_collection(self):
        """Missing WITH passes on single-server and fails on cluster (err 1521)."""
        for name, q in traversal_templates(LPG).items():
            if "OUTBOUND" in q or "INBOUND" in q:
                assert q.startswith("WITH nodes"), name

    def test_entry_point_query_uses_the_label_field(self):
        assert "@label IN n.labels" in traversal_templates(LPG)["nodes_by_label"]

    def test_traversals_carry_the_nested_index_hint(self):
        """The flat `indexHint: 'name'` form is silently ignored by traversals;
        only the nested per-collection/direction/level shape is honoured."""
        t = traversal_templates(LPG)
        assert "indexHint: {edges: {outbound: {base: ['r2g_lpg_vci_outbound']}}}" in (
            t["neighbors_by_type"]
        )
        assert "indexHint: {edges: {inbound: {base: ['r2g_lpg_vci_inbound']}}}" in (
            t["inbound_by_type"]
        )

    def test_hint_follows_the_configured_collection_name(self):
        hint = traversal_index_hint(LpgLayout(edge_collection="rel"), "outbound")
        assert hint.startswith("OPTIONS {indexHint: {rel: {outbound:")


class TestExplainAssertion:
    def test_detects_a_vertex_centric_index_in_a_plan(self):
        plan = {"plan": {"nodes": [{"indexes": [{"name": EDGE_OUTBOUND_INDEX}]}]}}
        assert uses_vertex_centric_index(plan) is True

    def test_rejects_a_plan_that_fell_back_to_the_edge_index(self):
        plan = {"plan": {"nodes": [{"indexes": [{"name": "edge"}]}]}}
        assert uses_vertex_centric_index(plan) is False

    def test_empty_plan_is_not_a_pass(self):
        assert uses_vertex_centric_index({}) is False


# ── pipeline routing (P13.6) ─────────────────────────────────────────


class _Session:
    def __init__(self, tables):
        self._t = tables

    def count_rows(self, table, *, since_column=None, since_value=None):
        return len(self._t.get(table, []))

    def stream_rows(self, table, *, batch_size=10_000, since_column=None, since_value=None):
        yield from self._t.get(table, [])

    def close(self):
        pass


class _Connector:
    connection_string = "fake://local"
    schema_name = "public"

    def __init__(self, tables):
        self._t = tables

    def get_schema(self):
        return Schema()

    def open_session(self):
        return _Session(self._t)


def _two_table_setup(layout: str):
    schema = Schema(tables={"accounts": _accounts(), "orders": _orders()})
    config = MappingConfig(source_schema="public", graph_layout=layout)
    config.collections = {
        "accounts": CollectionMapping(source_table="accounts", target_collection="Account"),
        "orders": CollectionMapping(source_table="orders", target_collection="Order"),
    }
    config.edges = [
        EdgeDefinition(
            edge_collection="placedBy",
            from_collection="orders",
            to_collection="accounts",
            from_fields=["account_id"],
            to_fields=["id"],
        )
    ]
    rows = {
        "accounts": [{"id": 1, "name": "Acme"}],
        "orders": [{"id": 10, "account_id": 1}],
    }
    return schema, config, _Connector(rows)


def _run(layout: str, writer, **kw):
    schema, config, connector = _two_table_setup(layout)
    StreamingPipeline(
        source_connector=connector,
        arango_writer=writer,
        schema=schema,
        config=config,
        **kw,
    ).run(graph_name="g")
    # collection name each import_batch targeted
    return [c.args[0] for c in writer.import_batch.call_args_list]


@pytest.fixture
def writer():
    w = MagicMock(spec=ArangoWriter)
    w.import_batch.return_value = {
        "created": 0, "errors": 0, "empty": 0, "updated": 0, "ignored": 0,
    }
    return w


class TestLpgPipelineRouting:
    def test_every_type_lands_in_the_single_collection_pair(self, writer):
        assert set(_run("lpg", writer)) == {"nodes", "edges"}

    def test_pg_layout_still_routes_per_type(self, writer):
        assert set(_run("pg", writer)) == {"Account", "Order", "placedBy"}

    def test_shared_collections_are_dropped_once_not_per_table(self, writer):
        """The hazard: a per-table drop would erase the tables loaded before it.

        Two tables + one edge — a per-table drop would fire three times and
        leave only the last table's rows, silently and without an error.
        """
        _run("lpg", writer, drop_collections=True)
        dropped = [c.args[0] for c in writer.drop_collection.call_args_list]
        assert sorted(dropped) == ["edges", "nodes"]

    def test_indexes_are_provisioned_with_the_right_edge_flag(self, writer):
        _run("lpg", writer)
        calls = {c.args[0]: c for c in writer.ensure_indexes.call_args_list}
        assert set(calls) == {"nodes", "edges"}
        # An edge collection created as a document collection succeeds quietly,
        # then graph creation fails ERR 1944 and traversals return nothing.
        assert calls["edges"].kwargs["edge"] is True
        assert calls["nodes"].kwargs["edge"] is False

    def test_pg_layout_provisions_no_lpg_indexes(self, writer):
        _run("pg", writer)
        writer.ensure_indexes.assert_not_called()

    def test_graph_gets_one_edge_definition(self, writer):
        _run("lpg", writer)
        defs = writer.create_named_graph.call_args.args[1]
        assert len(defs) == 1
        assert defs[0]["edge_collection"] == "edges"

    def test_documents_carry_labels_and_namespaced_keys(self, writer):
        _run("lpg", writer)
        docs = [
            d
            for call in writer.import_batch.call_args_list
            if call.args[0] == "nodes"
            for d in call.args[1]
        ]
        keys = {d["_key"] for d in docs}
        assert keys == {"Account_1", "Order_10"}
        assert {tuple(d["labels"]) for d in docs} == {("Account",), ("Order",)}

    def test_csi_describes_the_layout_it_produced(self, writer):
        """P13.7 — see TestLpgCsiExport; here only that routing and export agree."""
        _run("lpg", writer)
        _, config, _ = _two_table_setup("lpg")
        doc = mapping_to_csi(config, Schema(tables={"accounts": _accounts()}))
        assert doc["arangoPhysicalMapping"]["entities"]["Account"]["collectionName"] == "nodes"

    def test_edges_carry_type_and_endpoint_labels(self, writer):
        _run("lpg", writer)
        edges = [
            d
            for call in writer.import_batch.call_args_list
            if call.args[0] == "edges"
            for d in call.args[1]
        ]
        assert len(edges) == 1
        e = edges[0]
        assert e["type"] == "placedBy"
        assert e["fromLabels"] == ["Order"] and e["toLabels"] == ["Account"]
        assert e["_from"] == "nodes/Order_10" and e["_to"] == "nodes/Account_1"


# ── layout-aware CSI export (P13.7) ──────────────────────────────────


class TestLpgCsiExport:
    """The emitted CSI must describe the physical shape actually produced.

    Emitting the pg styles for an LPG target names collections that do not
    exist: a consumer resolves `Account` to a collection called `Account`,
    finds nothing, and fails far from the cause.
    """

    def _schema(self):
        return Schema(tables={"accounts": _accounts(), "orders": _orders()})

    def _doc(self, layout, target="Account"):
        config = MappingConfig(source_schema="public", graph_layout=layout)
        config.collections = {
            "accounts": CollectionMapping(source_table="accounts", target_collection=target),
            "orders": CollectionMapping(source_table="orders", target_collection="Order"),
        }
        config.edges = [
            EdgeDefinition(
                edge_collection="placedBy",
                from_collection="orders",
                to_collection="accounts",
                from_fields=["account_id"],
                to_fields=["id"],
            )
        ]
        return mapping_to_csi(config, self._schema())

    def test_pg_layout_is_unchanged(self):
        pm = self._doc("pg")["arangoPhysicalMapping"]
        assert pm["entities"]["Account"]["style"] == "COLLECTION"
        assert pm["entities"]["Account"]["collectionName"] == "Account"
        assert pm["relationships"]["placedBy"]["style"] == "DEDICATED_COLLECTION"
        assert "typeField" not in pm["entities"]["Account"]

    def test_lpg_entities_use_label_style_with_a_carrier(self):
        e = self._doc("lpg")["arangoPhysicalMapping"]["entities"]["Account"]
        assert e["style"] == "LABEL"
        assert e["collectionName"] == "nodes"
        assert e["typeField"] == "labels"
        assert e["typeValue"] == "Account"

    def test_lpg_relationships_use_generic_with_type(self):
        r = self._doc("lpg")["arangoPhysicalMapping"]["relationships"]["placedBy"]
        assert r["style"] == "GENERIC_WITH_TYPE"
        assert r["edgeCollectionName"] == "edges"
        assert r["typeField"] == "type"
        assert r["typeValue"] == "placedBy"
        # The CSI schema forbids collectionName on a relationship.
        assert "collectionName" not in r

    def test_type_value_is_the_stored_label_not_the_conceptual_name(self):
        """These diverge under a naming convention (`accounts` -> `Account`);
        the physical mapping must describe what the loader actually wrote."""
        doc = self._doc("lpg", target="accounts")
        entity = doc["conceptualModel"]["entities"][0]["name"]
        assert entity == "Account"  # conceptual, singular PascalCase
        assert doc["arangoPhysicalMapping"]["entities"]["Account"]["typeValue"] == "accounts"

    def test_both_layouts_validate_against_the_csi_schema(self):
        for layout in ("pg", "lpg"):
            validate_csi(self._doc(layout))


class TestLpgCdcRouting:
    """CDC deltas must land in the same shape the initial load produced (P13.9).

    A delta routed to a per-type collection would create a second, parallel
    graph beside the real one — and report success doing it.
    """

    def _cfg(self, layout):
        cfg = MappingConfig(source_schema="public", graph_layout=layout)
        cfg.collections = {
            "accounts": CollectionMapping(source_table="accounts", target_collection="Account"),
            "orders": CollectionMapping(source_table="orders", target_collection="Order"),
        }
        cfg.edges = [
            EdgeDefinition(
                edge_collection="placedBy",
                from_collection="orders",
                to_collection="accounts",
                from_fields=["account_id"],
                to_fields=["id"],
            )
        ]
        return cfg

    def _tx(self, layout):
        from r2g.cdc.delta_transformer import DeltaTransformer

        schema = Schema(tables={"accounts": _accounts(), "orders": _orders()})
        return DeltaTransformer(schema, self._cfg(layout))

    def _event(self, op, table="orders", new=None, old=None):
        from r2g.cdc.models import ChangeEvent, ChangeOperation

        return ChangeEvent(
            operation=getattr(ChangeOperation, op),
            table_name=table,
            new_row=new,
            old_row=old,
        )

    def test_insert_routes_to_the_shared_collections(self):
        deltas = self._tx("lpg").transform(
            self._event("INSERT", new={"id": 10, "account_id": 1})
        )
        assert {d.collection for d in deltas} == {"nodes", "edges"}

    def test_inserted_document_is_label_namespaced_and_labelled(self):
        deltas = self._tx("lpg").transform(
            self._event("INSERT", new={"id": 10, "account_id": 1})
        )
        doc = next(d for d in deltas if not d.is_edge).document
        assert doc["_key"] == "Order_10"
        assert doc["labels"] == ["Order"]

    def test_inserted_edge_carries_type_and_endpoint_labels(self):
        deltas = self._tx("lpg").transform(
            self._event("INSERT", new={"id": 10, "account_id": 1})
        )
        edge = next(d for d in deltas if d.is_edge).document
        assert edge["type"] == "placedBy"
        assert edge["_from"] == "nodes/Order_10"
        assert edge["_to"] == "nodes/Account_1"
        assert edge["fromLabels"] == ["Order"] and edge["toLabels"] == ["Account"]

    def test_delete_key_is_label_namespaced(self):
        """The subtle one: a delete carries only the old row, so its key is
        built outside NodeTransformer. Un-namespaced it would address
        `nodes/10` instead of `nodes/Order_10` — deleting nothing, or another
        table's document, with no trace either way since deletes are idempotent."""
        deltas = self._tx("lpg").transform(
            self._event("DELETE", old={"id": 10, "account_id": 1})
        )
        doc_delete = next(d for d in deltas if not d.is_edge)
        assert doc_delete.collection == "nodes"
        assert doc_delete.key == "Order_10"

    def test_delete_removes_the_edge_from_the_shared_collection(self):
        deltas = self._tx("lpg").transform(
            self._event("DELETE", old={"id": 10, "account_id": 1})
        )
        edge_delete = next(d for d in deltas if d.is_edge)
        assert edge_delete.collection == "edges"
        assert edge_delete.key.startswith("placedBy")

    def test_pg_layout_still_routes_per_type(self):
        deltas = self._tx("pg").transform(
            self._event("INSERT", new={"id": 10, "account_id": 1})
        )
        assert {d.collection for d in deltas} == {"Order", "placedBy"}
        doc = next(d for d in deltas if not d.is_edge).document
        assert doc["_key"] == "10"
        assert "labels" not in doc


class TestLabelPredicate:
    """The label-array filter forms, mapped onto Cypher's label expressions."""

    def test_has_one_label(self):
        assert label_predicate("e.toLabels") == "@label IN e.toLabels"

    def test_conjunction_matches_cypher_colon_a_colon_b(self):
        assert label_predicate("e.toLabels", mode="all", bind="ls") == "@ls ALL IN e.toLabels"

    def test_disjunction_matches_cypher_a_pipe_b(self):
        assert label_predicate("e.toLabels", mode="any", bind="ls") == "@ls ANY IN e.toLabels"

    def test_negation(self):
        assert label_predicate("n.labels", mode="none", bind="ls") == "@ls NONE IN n.labels"

    def test_unknown_mode_is_refused(self):
        with pytest.raises(ValueError, match="label mode"):
            label_predicate("e.toLabels", mode="most")

    def test_never_emits_the_nested_star_form(self):
        """`p.edges[*].toLabels` nests, so ALL ==/IN silently match nothing."""
        for mode in ("has", "all", "any", "none"):
            assert "[*]" not in label_predicate("e.toLabels", mode=mode)
