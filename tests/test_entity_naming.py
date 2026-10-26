"""Class names from the schema's own foreign keys (PRD P12.13, r2g-arango#30).

A column named after the table it references is the schema designer's word for
one row of that table. When every such column agrees on a singular that differs
from the singularizer's, it wins; otherwise the singularizer's name stands.
"""

from __future__ import annotations

from r2g.csi import entity_names_for_mapping, mapping_to_csi
from r2g.r2rml import mapping_to_r2rml
from r2g.types import CollectionMapping, Column, ForeignKey, MappingConfig, Schema, Table


def _table(name: str, *cols: str, fks=()) -> Table:
    columns = [Column(name="id", data_type="integer", is_primary_key=True)]
    columns += [Column(name=c, data_type="integer") for c in cols]
    return Table(name=name, columns=columns, primary_key=["id"], foreign_keys=list(fks))


def _fk(col: str, target: str, name: str | None = None) -> ForeignKey:
    return ForeignKey(columns=[col], foreign_table=target, foreign_columns=["id"], constraint_name=name)


def _mapping(schema: Schema, **renames: str) -> MappingConfig:
    return MappingConfig(
        collections={
            t: CollectionMapping(source_table=t, target_collection=renames.get(t, t)) for t in schema.tables
        },
        edges=[],
    )


def _goods(column: str, *, constraint: str | None = None, more=()) -> Schema:
    return Schema(
        tables={
            "goods": _table("goods"),
            "orders": _table("orders", column, fks=[_fk(column, "goods", constraint)]),
            **{t.name: t for t in more},
        }
    )


def test_a_column_named_after_the_table_keeps_the_designers_word():
    schema = _goods("goods_id")
    names, evidence = entity_names_for_mapping(_mapping(schema), schema)
    assert names["goods"] == "Goods"  # the dictionary alone gives Good
    assert evidence == [
        {
            "collection": "goods",
            "name": "Goods",
            "singularizerName": "Good",
            "rule": "foreign-key",
            "foreignKeys": ["orders.goods_id"],
        }
    ]


def test_agreement_with_the_singularizer_records_nothing():
    schema = _goods("good_id")
    names, evidence = entity_names_for_mapping(_mapping(schema), schema)
    assert names["goods"] == "Good"
    assert evidence == []


def test_role_names_are_not_evidence():
    # ship_via / reports_to / supplier_goods_id name a role, not the table.
    schema = Schema(
        tables={
            "goods": _table("goods"),
            "orders": _table(
                "orders", "ship_via", "reports_to", fks=[_fk("ship_via", "goods"), _fk("reports_to", "goods")]
            ),
            "lines": _table("lines", "supplier_goods_id", fks=[_fk("supplier_goods_id", "goods")]),
        }
    )
    names, evidence = entity_names_for_mapping(_mapping(schema), schema)
    assert names["goods"] == "Good" and evidence == []


def test_disagreeing_columns_leave_the_singularizer_in_charge():
    schema = _goods(
        "goods_id", more=[_table("lines", "good_id", fks=[_fk("good_id", "goods")])]
    )
    names, evidence = entity_names_for_mapping(_mapping(schema), schema)
    assert names["goods"] == "Good" and evidence == []


def test_inferred_keys_are_not_independent_evidence():
    # infer-fks matched them using the singularizer; counting them would be circular.
    schema = _goods("goods_id", constraint="inferred:orders_goods_id_fk")
    names, evidence = entity_names_for_mapping(_mapping(schema), schema)
    assert names["goods"] == "Good" and evidence == []


def test_a_renamed_collection_is_the_curators_choice():
    schema = _goods("goods_id")
    names, evidence = entity_names_for_mapping(_mapping(schema, goods="merchandise"), schema)
    assert names["merchandise"] == "Merchandise" and evidence == []


def test_compound_names_keep_their_head_words():
    schema = Schema(
        tables={
            "line_goods": _table("line_goods"),
            "orders": _table("orders", "line_goods_id", fks=[_fk("line_goods_id", "line_goods")]),
        }
    )
    names, _ = entity_names_for_mapping(_mapping(schema), schema)
    assert names["line_goods"] == "LineGoods"


def test_csi_and_r2rml_name_the_class_the_same_way():
    schema = _goods("goods_id")
    config = _mapping(schema)
    doc = mapping_to_csi(config, schema)
    assert "Goods" in {e["name"] for e in doc["conceptualModel"]["entities"]}
    assert doc["provenance"]["entityNaming"][0]["name"] == "Goods"
    ttl = mapping_to_r2rml(config, schema)
    assert "concept#Goods>" in ttl and "concept#Good>" not in ttl


def test_no_evidence_means_no_provenance_entry():
    schema = _goods("good_id")
    doc = mapping_to_csi(_mapping(schema), schema)
    assert "entityNaming" not in doc["provenance"]


def test_curated_labels_key_on_the_evidence_name():
    # A curator's entry names the class r2g generates, so it must see "Goods".
    schema = _goods("goods_id")
    doc = mapping_to_csi(
        _mapping(schema),
        schema,
        curated_labels={"curatedLabelsVersion": "1", "entities": [{"name": "Goods", "label": "Merchandise"}]},
    )
    assert doc["provenance"]["curatedLabels"][0]["resolution"] == "applied"
    assert "Merchandise" in {e["name"] for e in doc["conceptualModel"]["entities"]}
