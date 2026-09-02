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

from r2g.lpg import (
    EDGE_OUTBOUND_INDEX,
    VERTEX_LABEL_INDEX,
    edge_indexes,
    graph_edge_definition,
    indexes_used,
    traversal_templates,
    uses_vertex_centric_index,
    vertex_indexes,
)
from r2g.types import LpgLayout

from .conftest import requires_arango

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

    def test_traversal_uses_the_edge_index_not_the_vci(self, arango_test_db):
        """Measured behaviour on 3.12.9, recorded so it is not re-assumed.

        A traversal always picks the built-in ``edge`` index; `indexHint` and
        even `forceIndexHint` do not move it. If a future ArangoDB starts
        selecting the VCI, this test fails loudly and the layout's docs and
        templates should be revisited — that is the point of pinning it.
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
