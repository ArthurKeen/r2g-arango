"""LPG target layout (P13): one node collection + one edge collection.

The type stops being the collection and becomes data — ``labels`` on nodes,
``type`` on edges — plus copies of each edge's endpoint labels, which is what
makes a vertex-centric index able to filter a traversal from the index alone.

Two collapse hazards get explicit coverage here, because both lose data
*silently* rather than erroring: node ``_key`` collisions across former tables,
and edge ``_key`` collisions between two edge types joining the same pair.
"""

from __future__ import annotations

from r2g.transformers.edge_transformer import EdgeTransformer
from r2g.transformers.node_transformer import NodeTransformer
from r2g.types import (
    CollectionMapping,
    Column,
    EdgeDefinition,
    LpgLayout,
    MappingConfig,
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
