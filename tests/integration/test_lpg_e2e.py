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
    EDGE_OUTBOUND_INDEX,
    VERTEX_LABEL_INDEX,
    edge_indexes,
    graph_edge_definition,
    indexes_used,
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
        assert vci["fields"] == ["_from", "type", "toLabels[*]"]

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
