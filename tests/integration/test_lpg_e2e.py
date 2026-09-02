"""Live verification of the LPG layout's vertex-centric indexes (P13).

The copied endpoint labels on each edge exist for exactly one reason: to let a
type- and label-filtered traversal be answered *from the index*. That is a
performance claim, and a performance claim nothing checks is one that quietly
stops being true — rename a field, reorder a filter, and the plan silently
degrades to a full edge scan while every functional test still passes.

So these tests do not assert intent. They provision the real collections and
indexes, run ArangoDB's own ``explain``, and assert the plan actually names one
of the vertex-centric indexes.
"""

from __future__ import annotations

import os

import pytest

from r2g.connectors.arango_writer import ArangoWriter
from r2g.lpg import (
    EDGE_INBOUND_INDEX,
    EDGE_INBOUND_LABEL_INDEX,
    EDGE_OUTBOUND_INDEX,
    EDGE_OUTBOUND_LABEL_INDEX,
    VERTEX_LABEL_INDEX,
    edge_indexes,
    graph_edge_definition,
    indexes_used,
    label_predicate,
    traversal_index_hint,
    traversal_templates,
    uses_vertex_centric_index,
    vertex_indexes,
)
from r2g.streaming.pipeline import StreamingPipeline
from r2g.types import (
    CollectionMapping,
    Column,
    EdgeDefinition,
    LpgLayout,
    MappingConfig,
    Schema,
    Table,
)

from .conftest import (
    ARANGO_ENDPOINT,
    ARANGO_PASSWORD,
    ARANGO_USER,
    _arango_available,
    requires_arango,
)

#: The compose stack seeds northwind as the default database (docker-compose.yml),
#: which is a different DSN from the conftest's PG_CONN (an `r2g_test` database
#: that the stack does not create) — so these tests gate on the one they use.
PG_CONN_NORTHWIND = os.getenv(
    "PG_CONN_NORTHWIND", "postgresql://r2g:r2g_test_2026@localhost:5432/northwind"
)


def _northwind_available() -> bool:
    try:
        import psycopg

        with psycopg.connect(PG_CONN_NORTHWIND, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM information_schema.tables LIMIT 1")
        return True
    except Exception:
        return False


requires_northwind = pytest.mark.skipif(
    not (_northwind_available() and _arango_available()),
    reason="northwind Postgres + ArangoDB required",
)

LPG = LpgLayout()


def _provision(db):
    """Create the LPG collections + indexes and seed a tiny graph."""
    # The edge collection MUST be created with edge=True: as a document
    # collection this succeeds, then create_graph fails ERR 1944 and traversals
    # return nothing — a failure that surfaces far from its cause.
    nodes = db.create_collection(LPG.node_collection)
    edges = db.create_collection(LPG.edge_collection, edge=True)

    for spec in vertex_indexes(LPG):
        nodes.add_index(spec)
    for spec in edge_indexes(LPG):
        edges.add_index(spec)

    nodes.insert_many([
        {"_key": "Account_1", "labels": ["Account"], "name": "Acme"},
        {"_key": "Account_2", "labels": ["Account"], "name": "Globex"},
        {"_key": "Order_10", "labels": ["Order"], "total": 100},
        {"_key": "Order_11", "labels": ["Order"], "total": 250},
    ])
    edges.insert_many([
        {
            "_key": "placedBy_Order_10_Account_1",
            "_from": f"{LPG.node_collection}/Order_10",
            "_to": f"{LPG.node_collection}/Account_1",
            "type": "placedBy",
            "fromLabels": ["Order"],
            "toLabels": ["Account"],
        },
        {
            "_key": "billedTo_Order_10_Account_2",
            "_from": f"{LPG.node_collection}/Order_10",
            "_to": f"{LPG.node_collection}/Account_2",
            "type": "billedTo",
            "fromLabels": ["Order"],
            "toLabels": ["Account"],
        },
    ])
    db.create_graph("lpg_graph", edge_definitions=[graph_edge_definition(LPG)])
    return nodes, edges


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


@requires_arango
class TestLpgEndToEndLoad:
    """P13.6: the pipeline actually lands an LPG graph in a real ArangoDB.

    Fake *source*, real *target* — the point is the write path and the graph it
    produces, not the relational read.
    """

    def _load(self, db_name):
        schema = Schema(
            tables={
                "accounts": Table(
                    name="accounts",
                    columns=[
                        Column(name="id", data_type="integer", is_primary_key=True),
                        Column(name="name", data_type="text"),
                    ],
                    primary_key=["id"],
                ),
                "orders": Table(
                    name="orders",
                    columns=[
                        Column(name="id", data_type="integer", is_primary_key=True),
                        Column(name="account_id", data_type="integer"),
                    ],
                    primary_key=["id"],
                ),
            }
        )
        config = MappingConfig(source_schema="public", graph_layout="lpg")
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
            "accounts": [{"id": 1, "name": "Acme"}, {"id": 2, "name": "Globex"}],
            "orders": [{"id": 10, "account_id": 1}, {"id": 11, "account_id": 2}],
        }
        writer = ArangoWriter(
            endpoint=ARANGO_ENDPOINT,
            database=db_name,
            username=ARANGO_USER,
            password=ARANGO_PASSWORD,
        )
        StreamingPipeline(
            source_connector=_Connector(rows),
            arango_writer=writer,
            schema=schema,
            config=config,
        ).run(graph_name="lpg_graph")

    def test_load_produces_one_node_and_one_edge_collection(self, arango_test_db):
        name, db = arango_test_db
        self._load(name)
        names = {c["name"] for c in db.collections() if not c["name"].startswith("_")}
        assert names == {LPG.node_collection, LPG.edge_collection}
        assert db.collection(LPG.node_collection).count() == 4  # 2 accounts + 2 orders
        assert db.collection(LPG.edge_collection).count() == 2

    def test_loaded_documents_carry_labels_and_namespaced_keys(self, arango_test_db):
        name, db = arango_test_db
        self._load(name)
        keys = {d["_key"] for d in db.collection(LPG.node_collection).all()}
        assert keys == {"Account_1", "Account_2", "Order_10", "Order_11"}

    def test_traversal_over_the_loaded_graph_works_and_uses_the_vci(self, arango_test_db):
        """End-to-end payoff: loaded by the pipeline, traversed by the template,
        filtered through the vertex-centric index."""
        name, db = arango_test_db
        self._load(name)
        q = traversal_templates(LPG)["neighbors_by_type"]
        bind = {
            "start": f"{LPG.node_collection}/Order_10",
            "type": "placedBy",
            "toLabel": "Account",
        }
        assert uses_vertex_centric_index(db.aql.explain(q, bind_vars=bind))
        rows = list(db.aql.execute(q, bind_vars=bind))
        assert [r["_key"] for r in rows] == ["Account_1"]


@requires_arango
class TestLpgLiveIndexes:
    def test_indexes_are_created_as_specified(self, arango_test_db):
        _, db = arango_test_db
        _, edges = _provision(db)
        by_name = {i.get("name"): i for i in edges.indexes()}
        assert EDGE_OUTBOUND_INDEX in by_name
        vci = by_name[EDGE_OUTBOUND_INDEX]
        assert vci["type"] == "persistent"
        # No array expansion: indexing toLabels[*] makes a traversal return
        # each edge once per label on its target (see
        # TestLpgNoDuplicationFromLabelArrays).
        assert vci["fields"] == ["_from", "type"]

    def test_traversal_actually_uses_the_vertex_centric_index(self, arango_test_db):
        """The load-bearing assertion for the whole layout."""
        _, db = arango_test_db
        _provision(db)
        explained = db.aql.explain(
            traversal_templates(LPG)["neighbors_by_type"],
            bind_vars={
                "start": f"{LPG.node_collection}/Order_10",
                "type": "placedBy",
                "toLabel": "Account",
            },
        )
        assert uses_vertex_centric_index(explained), (
            f"traversal fell back to {indexes_used(explained)} — the VCI was not used"
        )

    def test_inbound_traversal_uses_its_own_vci(self, arango_test_db):
        _, db = arango_test_db
        _provision(db)
        explained = db.aql.explain(
            traversal_templates(LPG)["inbound_by_type"],
            bind_vars={
                "start": f"{LPG.node_collection}/Account_1",
                "depth": 1,
                "type": "placedBy",
                "fromLabel": "Order",
            },
        )
        assert EDGE_INBOUND_INDEX in indexes_used(explained)

    def test_without_the_hint_it_silently_falls_back(self, arango_test_db):
        """Why the hint is generated rather than left to the optimizer.

        The flat ``indexHint: 'name'`` form is silently ignored by traversals —
        no error, no effect — so an unhinted traversal degrades to the built-in
        edge index with identical results and very different cost.
        """
        _, db = arango_test_db
        _provision(db)
        unhinted = (
            f"WITH {LPG.node_collection}\n"
            f"FOR v, e IN 1..1 OUTBOUND @start {LPG.edge_collection}\n"
            f"  FILTER e.type == @type RETURN v"
        )
        explained = db.aql.explain(
            unhinted,
            bind_vars={"start": f"{LPG.node_collection}/Order_10", "type": "placedBy"},
        )
        assert indexes_used(explained) == ["edge"]
        assert not uses_vertex_centric_index(explained)

    def test_traversal_filters_from_the_edge_alone(self, arango_test_db):
        """The real traversal win from copying labels onto the edge.

        ``type`` and ``toLabels`` appear as edge projections, so the filter is
        answered from the edge document and non-matching neighbours are never
        materialized. Were the labels left on the neighbour node, every
        candidate vertex would have to be loaded to be rejected.
        """
        _, db = arango_test_db
        _provision(db)
        explained = db.aql.explain(
            traversal_templates(LPG)["neighbors_by_type"],
            bind_vars={
                "start": f"{LPG.node_collection}/Order_10",
                "type": "placedBy",
                "toLabel": "Account",
            },
        )
        projected = {
            "/".join(p["path"])
            for node in explained["nodes"]
            for p in (node.get("options", {}).get("edgeProjections") or [])
        }
        assert {"type", "toLabels"} <= projected

    def test_vci_is_usable_on_the_pattern_match_path(self, arango_test_db):
        """The VCI is a valid, usable index — just not for traversals.

        Forcing it proves the index shape is correct, so the provisioning is
        not dead weight: the one-hop pattern-match access path can use it.
        """
        _, db = arango_test_db
        _provision(db)
        explained = db.aql.explain(
            "FOR e IN edges OPTIONS {indexHint: @hint, forceIndexHint: true} "
            "FILTER e._from == @start AND e.type == @type RETURN e",
            bind_vars={
                "hint": EDGE_OUTBOUND_INDEX,
                "start": f"{LPG.node_collection}/Order_10",
                "type": "placedBy",
            },
        )
        assert uses_vertex_centric_index(explained)

    def test_type_filter_actually_restricts(self, arango_test_db):
        """Guards the `ALL ==` trap: a filter that indexes well but matches
        everything would pass the explain check and still be wrong."""
        _, db = arango_test_db
        _provision(db)
        query = traversal_templates(LPG)["neighbors_by_type"]
        rows = list(db.aql.execute(
            query,
            bind_vars={
                "start": f"{LPG.node_collection}/Order_10",
                "type": "placedBy",
                "toLabel": "Account",
            },
        ))
        # Order_10 has two edges; only one is placedBy.
        assert len(rows) == 1
        assert rows[0]["_key"] == "Account_1"

    def test_label_entry_point_uses_the_vertex_label_index(self, arango_test_db):
        _, db = arango_test_db
        _provision(db)
        explained = db.aql.explain(
            traversal_templates(LPG)["nodes_by_label"],
            bind_vars={"label": "Account", "limit": 10},
        )
        # The entry point IS index-backed — verified, not assumed.
        assert VERTEX_LABEL_INDEX in indexes_used(explained)


@requires_northwind
class TestLpgRealSourceEndToEnd:
    """The full chain: a REAL relational database -> LPG graph -> traversal.

    The other end-to-end test in this module drives a fake source, which proves
    the write path but never exercises real introspection, a generated mapping,
    composite/self-referential FKs, or realistic volume. This one does, because
    "it works end to end" should not rest on a hand-built two-table fixture.
    """

    def _load(self, db_name, **pipeline_kw):
        from r2g.config import ConfigManager
        from r2g.connectors.postgres import PostgresConnector

        conn = PostgresConnector(PG_CONN_NORTHWIND, schema_name="public")
        schema = conn.get_schema()
        config = ConfigManager.generate_default_config(schema)
        config.graph_layout = "lpg"
        writer = ArangoWriter(
            endpoint=ARANGO_ENDPOINT,
            database=db_name,
            username=ARANGO_USER,
            password=ARANGO_PASSWORD,
        )
        StreamingPipeline(
            source_connector=conn,
            arango_writer=writer,
            schema=schema,
            config=config,
            batch_size=500,
            **pipeline_kw,
        ).run(graph_name="nw_lpg")
        return schema, config

    def test_whole_database_collapses_into_two_collections(self, arango_test_db):
        name, db = arango_test_db
        schema, config = self._load(name)
        colls = {c["name"] for c in db.collections() if not c["name"].startswith("_")}
        assert colls == {LPG.node_collection, LPG.edge_collection}
        # Many tables and many FK types, two collections.
        assert len(config.collections) > 5 and len(config.edges) > 5
        assert db.collection(LPG.node_collection).count() > 1000
        assert db.collection(LPG.edge_collection).count() > 1000

    def test_labels_and_types_survive_the_collapse(self, arango_test_db):
        """Every former collection must still be identifiable by label/type —
        that is the only thing standing in for the collections we gave up."""
        name, db = arango_test_db
        schema, config = self._load(name)
        labels = set(db.aql.execute("FOR n IN nodes COLLECT l = n.labels[0] RETURN l"))
        types = set(db.aql.execute("FOR e IN edges COLLECT t = e.type RETURN t"))
        # Every non-empty mapped collection appears as a label.
        assert labels <= {c.target_collection for c in config.collections.values()}
        assert len(labels) > 5
        assert types <= {e.edge_collection for e in config.edges}
        assert len(types) > 5

    def test_no_key_collisions_across_tables(self, arango_test_db):
        """The collapse hazard, at real volume: label-namespaced keys mean the
        node count equals the sum of rows, with nothing silently overwritten."""
        name, db = arango_test_db
        self._load(name)
        dupes = list(db.aql.execute(
            "FOR n IN nodes COLLECT k = n._key WITH COUNT INTO c "
            "FILTER c > 1 LIMIT 1 RETURN k"
        ))
        assert dupes == []

    def test_named_graph_has_exactly_one_edge_definition(self, arango_test_db):
        name, db = arango_test_db
        self._load(name)
        defs = db.graph("nw_lpg").edge_definitions()
        assert len(defs) == 1
        assert defs[0]["edge_collection"] == LPG.edge_collection
        assert defs[0]["from_vertex_collections"] == [LPG.node_collection]

    def test_traversal_at_volume_uses_the_vci_and_filters_correctly(self, arango_test_db):
        name, db = arango_test_db
        self._load(name)
        seed = list(db.aql.execute(
            "FOR e IN edges COLLECT t = e.type, f = e._from, tl = e.toLabels[0] "
            "LIMIT 1 RETURN {t: t, f: f, tl: tl}"
        ))[0]
        q = traversal_templates(LPG)["neighbors_by_type"]
        bind = {"start": seed["f"], "type": seed["t"], "toLabel": seed["tl"]}
        assert uses_vertex_centric_index(db.aql.explain(q, bind_vars=bind))
        rows = list(db.aql.execute(q, bind_vars=bind))
        assert rows
        assert all(seed["tl"] in r.get("labels", []) for r in rows)

    def test_reload_with_drop_collections_does_not_lose_tables(self, arango_test_db):
        """--drop-collections must drop the shared pair ONCE per run. Dropping
        per table would leave only the last table's rows — silently."""
        name, db = arango_test_db
        self._load(name)
        first = db.collection(LPG.node_collection).count()
        self._load(name, drop_collections=True)
        assert db.collection(LPG.node_collection).count() == first


@requires_arango
class TestLpgCdcLive:
    """P13.9 live: CDC deltas keep an LPG graph in ONE shape.

    The failure this guards against is quiet: deltas routed to per-type
    collections would build a second graph beside the real one and report
    success, and an un-namespaced delete key would remove nothing (deletes are
    idempotent, so the miss leaves no trace). Both are only visible by looking
    at the database afterwards.
    """

    def _tx_and_writer(self, db_name):
        from r2g.cdc.delta_transformer import DeltaTransformer
        from r2g.types import MappingConfig as MC

        schema = Schema(
            tables={
                "accounts": Table(
                    name="accounts",
                    columns=[
                        Column(name="id", data_type="integer", is_primary_key=True),
                        Column(name="name", data_type="text"),
                    ],
                    primary_key=["id"],
                ),
                "orders": Table(
                    name="orders",
                    columns=[
                        Column(name="id", data_type="integer", is_primary_key=True),
                        Column(name="account_id", data_type="integer"),
                    ],
                    primary_key=["id"],
                ),
            }
        )
        config = MC(source_schema="public", graph_layout="lpg")
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
        writer = ArangoWriter(
            endpoint=ARANGO_ENDPOINT,
            database=db_name,
            username=ARANGO_USER,
            password=ARANGO_PASSWORD,
        )
        writer.connect()
        return DeltaTransformer(schema, config), writer

    def _event(self, op, table, new=None, old=None):
        from r2g.cdc.models import ChangeEvent, ChangeOperation

        return ChangeEvent(
            operation=getattr(ChangeOperation, op),
            table_name=table,
            new_row=new,
            old_row=old,
        )

    def test_cdc_creates_no_extra_collections(self, arango_test_db):
        name, db = arango_test_db
        tx, writer = self._tx_and_writer(name)
        for d in tx.transform(self._event("INSERT", "accounts", new={"id": 1, "name": "Acme"})):
            writer.apply_delta(d)
        for d in tx.transform(self._event("INSERT", "orders", new={"id": 10, "account_id": 1})):
            writer.apply_delta(d)
        colls = {c["name"] for c in db.collections() if not c["name"].startswith("_")}
        # No `Account` / `Order` / `placedBy` collections beside the real graph.
        assert colls == {LPG.node_collection, LPG.edge_collection}

    def test_cdc_insert_is_reachable_by_traversal(self, arango_test_db):
        name, db = arango_test_db
        tx, writer = self._tx_and_writer(name)
        for tbl, row in (("accounts", {"id": 1, "name": "Acme"}),
                         ("orders", {"id": 10, "account_id": 1})):
            for d in tx.transform(self._event("INSERT", tbl, new=row)):
                writer.apply_delta(d)
        rows = list(db.aql.execute(
            traversal_templates(LPG)["neighbors_by_type"],
            bind_vars={"start": f"{LPG.node_collection}/Order_10",
                       "type": "placedBy", "toLabel": "Account"},
        ))
        assert [r["_key"] for r in rows] == ["Account_1"]

    def test_cdc_delete_actually_deletes(self, arango_test_db):
        """Proves the delete key carries its label namespace: an un-namespaced
        key would target nodes/10, silently remove nothing, and leave the
        document sitting here."""
        name, db = arango_test_db
        tx, writer = self._tx_and_writer(name)
        for d in tx.transform(self._event("INSERT", "orders", new={"id": 10, "account_id": 1})):
            writer.apply_delta(d)
        assert db.collection(LPG.node_collection).has("Order_10")
        for d in tx.transform(self._event("DELETE", "orders", old={"id": 10, "account_id": 1})):
            writer.apply_delta(d)
        assert not db.collection(LPG.node_collection).has("Order_10")


@requires_arango
class TestLpgMultiLabel:
    """Multi-label vertices: the case the `labels` array exists for.

    r2g's loader writes exactly one label per node today, so these tests seed
    multi-label documents directly — the point is that the *layout and its
    indexes* stay correct when a node carries several labels, which is what a
    consumer (or a future multi-label mapping) will rely on.
    """

    def _seed(self, db):
        nodes = db.create_collection(LPG.node_collection)
        edges = db.create_collection(LPG.edge_collection, edge=True)
        for spec in vertex_indexes(LPG):
            nodes.add_index(spec)
        for spec in edge_indexes(LPG):
            edges.add_index(spec)
        nodes.insert_many([
            {"_key": "O_1", "labels": ["Order"]},
            {"_key": "A_1", "labels": ["Account", "Customer", "Premium"]},
            {"_key": "A_2", "labels": ["Account"]},
        ])
        edges.insert_many([
            {"_from": "nodes/O_1", "_to": "nodes/A_1", "type": "placedBy",
             "fromLabels": ["Order"], "toLabels": ["Account", "Customer", "Premium"]},
            {"_from": "nodes/O_1", "_to": "nodes/A_2", "type": "placedBy",
             "fromLabels": ["Order"], "toLabels": ["Account"]},
        ])
        return nodes, edges

    def _traverse(self, db, predicate, bind):
        q = (
            f"WITH {LPG.node_collection}\n"
            f"FOR v, e IN 1..1 OUTBOUND @start {LPG.edge_collection}\n"
            f"  {traversal_index_hint(LPG, 'outbound')}\n"
            f"  FILTER e.type == @type\n"
            f"  FILTER {predicate}\n"
            f"  RETURN v._key"
        )
        bind_vars = {"start": f"{LPG.node_collection}/O_1", "type": "placedBy", **bind}
        return sorted(db.aql.execute(q, bind_vars=bind_vars)), db.aql.explain(q, bind_vars=bind_vars)

    def test_single_label_test_finds_multi_label_nodes(self, arango_test_db):
        _, db = arango_test_db
        self._seed(db)
        rows, ex = self._traverse(db, label_predicate("e.toLabels"), {"label": "Account"})
        assert rows == ["A_1", "A_2"]  # exactly once each: A_1 has 3 labels
        assert uses_vertex_centric_index(ex)     # still index-served

    def test_a_label_unique_to_the_multi_label_node(self, arango_test_db):
        _, db = arango_test_db
        self._seed(db)
        rows, ex = self._traverse(db, label_predicate("e.toLabels"), {"label": "Premium"})
        assert rows == ["A_1"]
        assert uses_vertex_centric_index(ex)

    def test_conjunction_requires_every_label(self, arango_test_db):
        """Cypher `:Account:Premium` — A_2 has Account but not Premium."""
        _, db = arango_test_db
        self._seed(db)
        rows, ex = self._traverse(
            db, label_predicate("e.toLabels", mode="all", bind="ls"),
            {"ls": ["Account", "Premium"]},
        )
        assert rows == ["A_1"]
        assert uses_vertex_centric_index(ex)

    def test_disjunction_accepts_either_label(self, arango_test_db):
        """Cypher `:Premium|Customer`."""
        _, db = arango_test_db
        self._seed(db)
        rows, _ = self._traverse(
            db, label_predicate("e.toLabels", mode="any", bind="ls"),
            {"ls": ["Premium", "Nonexistent"]},
        )
        assert rows == ["A_1"]

    def test_negation_excludes_the_labelled_node(self, arango_test_db):
        _, db = arango_test_db
        self._seed(db)
        rows, _ = self._traverse(
            db, label_predicate("e.toLabels", mode="none", bind="ls"), {"ls": ["Premium"]},
        )
        assert rows == ["A_2"]

    def test_vertex_label_index_serves_a_multi_label_array(self, arango_test_db):
        _, db = arango_test_db
        self._seed(db)
        q = traversal_templates(LPG)["nodes_by_label"]
        ex = db.aql.explain(q, bind_vars={"label": "Premium", "limit": 10})
        assert VERTEX_LABEL_INDEX in indexes_used(ex)
        rows = [d["_key"] for d in db.aql.execute(q, bind_vars={"label": "Premium", "limit": 10})]
        assert rows == ["A_1"]

    def test_the_star_path_form_is_silently_wrong(self, arango_test_db):
        """Why label_predicate refuses to emit `p.edges[*]`.

        For a LIST-valued field the `[*]` form yields a NESTED array, so both
        the `ALL ==` and the `IN` shapes match nothing — and return an empty
        result rather than an error. A transpiler emitting either would produce
        queries that look right and quietly answer nothing.
        """
        _, db = arango_test_db
        self._seed(db)
        base = (
            f"WITH {LPG.node_collection}\n"
            f"FOR v, e, p IN 1..1 OUTBOUND @start {LPG.edge_collection}\n  FILTER "
        )
        bind = {"start": f"{LPG.node_collection}/O_1", "label": "Premium"}
        broken_all = base + "p.edges[*].toLabels ALL == @label RETURN v._key"
        broken_in = base + "@label IN p.edges[*].toLabels RETURN v._key"
        assert list(db.aql.execute(broken_all, bind_vars=bind)) == []
        assert list(db.aql.execute(broken_in, bind_vars=bind)) == []
        # The correct per-hop forms, for contrast.
        ok_positional = base + "@label IN p.edges[0].toLabels RETURN v._key"
        ok_edge_var = base + "@label IN e.toLabels RETURN v._key"
        assert list(db.aql.execute(ok_positional, bind_vars=bind)) == ["A_1"]
        assert list(db.aql.execute(ok_edge_var, bind_vars=bind)) == ["A_1"]


@requires_arango
class TestLpgMultiHopSemantics:
    """Depth > 1: `e` is the FINAL edge, not every edge.

    A `FILTER` on `e` therefore constrains only the last hop and lets paths
    through non-matching earlier edges — which is why the depth-ranged
    templates use the `[*] ALL ==` form for the edge type. On a SCALAR field
    that form is both correct and pushed into the traverser as a global edge
    condition served by the VCI; the earlier `e`-based template silently
    over-returned.
    """

    def _seed(self, db):
        nodes = db.create_collection(LPG.node_collection)
        edges = db.create_collection(LPG.edge_collection, edge=True)
        for spec in vertex_indexes(LPG):
            nodes.add_index(spec)
        for spec in edge_indexes(LPG):
            edges.add_index(spec)
        nodes.insert_many([
            {"_key": "O_1", "labels": ["Order"]},
            {"_key": "M_1", "labels": ["Mid"]}, {"_key": "M_2", "labels": ["Mid"]},
            {"_key": "A_1", "labels": ["Account"]}, {"_key": "A_2", "labels": ["Account"]},
        ])
        # A_1 is reached by good->good; A_2 by bad->good.
        edges.insert_many([
            {"_from": "nodes/O_1", "_to": "nodes/M_1", "type": "good",
             "fromLabels": ["Order"], "toLabels": ["Mid"]},
            {"_from": "nodes/M_1", "_to": "nodes/A_1", "type": "good",
             "fromLabels": ["Mid"], "toLabels": ["Account"]},
            {"_from": "nodes/O_1", "_to": "nodes/M_2", "type": "bad",
             "fromLabels": ["Order"], "toLabels": ["Mid"]},
            {"_from": "nodes/M_2", "_to": "nodes/A_2", "type": "good",
             "fromLabels": ["Mid"], "toLabels": ["Account"]},
        ])

    def _bind(self):
        return {"start": f"{LPG.node_collection}/O_1", "depth": 2,
                "type": "good", "toLabel": "Account"}

    def test_every_hop_must_match_the_type(self, arango_test_db):
        _, db = arango_test_db
        self._seed(db)
        rows = sorted(
            r["vertex"]["_key"]
            for r in db.aql.execute(traversal_templates(LPG)["outbound_by_type"],
                                    bind_vars=self._bind())
        )
        # A_2 sits behind a 'bad' first hop and must not be returned.
        assert rows == ["A_1"]

    def test_the_all_form_is_pushed_into_the_traverser(self, arango_test_db):
        """`[*] ALL ==` on a scalar is not a post-filter: it becomes a global
        edge condition and is served by the vertex-centric index."""
        _, db = arango_test_db
        self._seed(db)
        ex = db.aql.explain(traversal_templates(LPG)["outbound_by_type"],
                            bind_vars=self._bind())
        assert any(node.get("globalEdgeConditions")
                   for node in ex["nodes"] if node["type"] == "TraversalNode")
        assert uses_vertex_centric_index(ex)
        assert not [n for n in ex["nodes"] if n["type"] == "FilterNode"]

    def test_an_edge_variable_filter_would_leak(self, arango_test_db):
        """Pins the bug the template fix removed, so it cannot return."""
        _, db = arango_test_db
        self._seed(db)
        leaky = (
            f"WITH {LPG.node_collection}\n"
            f"FOR v, e IN 1..@depth OUTBOUND @start {LPG.edge_collection}\n"
            f"  FILTER e.type == @type FILTER @toLabel IN e.toLabels RETURN v._key"
        )
        assert sorted(db.aql.execute(leaky, bind_vars=self._bind())) == ["A_1", "A_2"]


@requires_arango
class TestLpgNoDuplicationFromLabelArrays:
    """An array-expansion field in a vertex-centric index duplicates traversals.

    A persistent index stores one entry per array element, and a traversal scans
    every entry under the `_from`/`type` prefix — so indexing `toLabels[*]` makes
    each edge come back once per label on its target. The multiplier is the
    label count, which is exactly what multi-label support introduces, and the
    symptom is inflated counts rather than an error. `RETURN DISTINCT` hides it,
    which is why these assertions count occurrences instead.
    """

    def _seed(self, db):
        nodes = db.create_collection(LPG.node_collection)
        edges = db.create_collection(LPG.edge_collection, edge=True)
        for spec in vertex_indexes(LPG):
            nodes.add_index(spec)
        for spec in edge_indexes(LPG):
            edges.add_index(spec)
        nodes.insert_many([
            {"_key": "O_1", "labels": ["Order"]},
            {"_key": "L_1", "labels": ["A"]},
            {"_key": "L_2", "labels": ["A", "B"]},
            {"_key": "L_3", "labels": ["A", "B", "C"]},
        ])
        edges.insert_many([
            {"_from": "nodes/O_1", "_to": "nodes/L_1", "type": "t",
             "fromLabels": ["Order"], "toLabels": ["A"]},
            {"_from": "nodes/O_1", "_to": "nodes/L_2", "type": "t",
             "fromLabels": ["Order"], "toLabels": ["A", "B"]},
            {"_from": "nodes/O_1", "_to": "nodes/L_3", "type": "t",
             "fromLabels": ["Order"], "toLabels": ["A", "B", "C"]},
        ])

    def _counts(self, db, hinted):
        hint = traversal_index_hint(LPG, "outbound") if hinted else ""
        q = (
            f"WITH {LPG.node_collection}\n"
            f"FOR v, e IN 1..1 OUTBOUND @start {LPG.edge_collection} {hint}\n"
            f"  FILTER e.type == @type\n  RETURN v._key"
        )
        rows = list(db.aql.execute(
            q, bind_vars={"start": f"{LPG.node_collection}/O_1", "type": "t"}))
        return {k: rows.count(k) for k in set(rows)}

    def test_every_edge_is_traversed_exactly_once(self, arango_test_db):
        """Targets carry 1, 2 and 3 labels; each must appear exactly once."""
        _, db = arango_test_db
        self._seed(db)
        assert self._counts(db, hinted=True) == {"L_1": 1, "L_2": 1, "L_3": 1}

    def test_holds_without_the_index_hint_too(self, arango_test_db):
        _, db = arango_test_db
        self._seed(db)
        assert self._counts(db, hinted=False) == {"L_1": 1, "L_2": 1, "L_3": 1}

    def test_the_edge_indexes_do_not_expand_an_array(self, arango_test_db):
        """The structural guard: no edge index may carry a `[*]` field, since
        that is what reintroduces the multiplier."""
        _, db = arango_test_db
        self._seed(db)
        for idx in db.collection(LPG.edge_collection).indexes():
            assert not any("[*]" in f for f in idx.get("fields", [])), idx.get("name")

    def test_vertex_label_index_still_expands_and_does_not_duplicate(self, arango_test_db):
        """The array expansion is fine on the vertex collection: an equality
        lookup matches one entry per document, via an IndexNode not a traversal."""
        _, db = arango_test_db
        self._seed(db)
        rows = list(db.aql.execute(
            traversal_templates(LPG)["nodes_by_label"],
            bind_vars={"label": "A", "limit": 10}))
        keys = [d["_key"] for d in rows]
        assert sorted(keys) == ["L_1", "L_2", "L_3"]
        assert len(keys) == len(set(keys))


@requires_arango
class TestOptInLabelIndexLive:
    """`index_edge_labels` provisions an index that helps pattern-match queries
    and would duplicate traversals — so the point of these tests is that having
    BOTH indexes present is safe: each access path takes the right one."""

    LPG_OPT = LpgLayout(index_edge_labels=True)

    def _seed(self, db):
        nodes = db.create_collection(self.LPG_OPT.node_collection)
        edges = db.create_collection(self.LPG_OPT.edge_collection, edge=True)
        for spec in vertex_indexes(self.LPG_OPT):
            nodes.add_index(spec)
        for spec in edge_indexes(self.LPG_OPT):
            edges.add_index(spec)
        nodes.insert_many([{"_key": k, "labels": ["X"]} for k in ("O", "L1", "L2", "L3")])
        edges.insert_many([
            {"_from": "nodes/O", "_to": "nodes/L1", "type": "t", "toLabels": ["A"]},
            {"_from": "nodes/O", "_to": "nodes/L2", "type": "t", "toLabels": ["A", "B"]},
            {"_from": "nodes/O", "_to": "nodes/L3", "type": "t", "toLabels": ["A", "B", "C"]},
        ])
        return edges

    def test_all_four_edge_indexes_are_provisioned(self, arango_test_db):
        _, db = arango_test_db
        edges = self._seed(db)
        names = {i.get("name") for i in edges.indexes()}
        assert {EDGE_OUTBOUND_INDEX, EDGE_INBOUND_INDEX,
                EDGE_OUTBOUND_LABEL_INDEX, EDGE_INBOUND_LABEL_INDEX} <= names

    def test_traversal_still_takes_the_flat_index_and_does_not_duplicate(self, arango_test_db):
        """The safety property: the explicit hint pins the traversal to the flat
        index, so opting in cannot silently inflate traversal counts."""
        _, db = arango_test_db
        self._seed(db)
        q = traversal_templates(self.LPG_OPT)["neighbors_by_type"]
        bind = {"start": "nodes/O", "type": "t", "toLabel": "A"}
        assert EDGE_OUTBOUND_INDEX in indexes_used(db.aql.explain(q, bind_vars=bind))
        keys = [r["_key"] for r in db.aql.execute(q, bind_vars=bind)]
        assert sorted(keys) == ["L1", "L2", "L3"]   # once each, despite 1/2/3 labels

    def test_pattern_match_query_takes_the_label_index(self, arango_test_db):
        """What the opt-in is for: a label-filtered edge lookup, index-served."""
        _, db = arango_test_db
        self._seed(db)
        q = ("FOR e IN edges FILTER e._from == @s AND e.type == @t "
             "AND @l IN e.toLabels RETURN e._to")
        bind = {"s": "nodes/O", "t": "t", "l": "A"}
        assert EDGE_OUTBOUND_LABEL_INDEX in indexes_used(db.aql.explain(q, bind_vars=bind))
        rows = list(db.aql.execute(q, bind_vars=bind))
        assert len(rows) == len(set(rows)) == 3
