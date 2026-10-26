"""Class names from the schema's own foreign keys (PRD P12.13, r2g-arango#30).

A column that names one row of the table it references with a singular of its
own (``plans.basis_id`` -> ``bases``) is the schema designer's word for that
row. When every such column agrees on a singular that differs from the
singularizer's, it wins; otherwise the singularizer's name stands. A column that
only repeats its table's name (``goods_id``, ``customers_id``) is not evidence.
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


def _bases(column: str, *, constraint: str | None = None, more=()) -> Schema:
    # The singularizer gives bases -> Base; basis_id says one row is a basis.
    return Schema(
        tables={
            "bases": _table("bases"),
            "plans": _table("plans", column, fks=[_fk(column, "bases", constraint)]),
            **{t.name: t for t in more},
        }
    )


def test_a_column_with_its_own_singular_keeps_the_designers_word():
    schema = _bases("basis_id")
    names, evidence = entity_names_for_mapping(_mapping(schema), schema)
    assert names["bases"] == "Basis"
    assert evidence == [
        {
            "collection": "bases",
            "name": "Basis",
            "singularizerName": "Base",
            "rule": "foreign-key",
            "foreignKeys": ["plans.basis_id"],
        }
    ]


def test_agreement_with_the_singularizer_records_nothing():
    schema = _bases("base_id")
    names, evidence = entity_names_for_mapping(_mapping(schema), schema)
    assert names["bases"] == "Base"
    assert evidence == []


def test_a_column_that_repeats_the_table_name_is_not_evidence():
    # goods_id looks exactly like the <table>_id habit (customers_id), even in a
    # schema that otherwise writes singular keys. A curated identifier names it.
    schema = Schema(
        tables={
            "customers": _table("customers"),
            "goods": _table("goods"),
            "orders": _table(
                "orders",
                "customer_id",
                "goods_id",
                fks=[_fk("customer_id", "customers"), _fk("goods_id", "goods")],
            ),
            "plans": _table("plans", "bases_id", fks=[_fk("bases_id", "bases")]),
            "bases": _table("bases"),
        }
    )
    names, evidence = entity_names_for_mapping(_mapping(schema), schema)
    assert (names["goods"], names["bases"], names["customers"]) == ("Good", "Base", "Customer")
    assert evidence == []


def test_a_schema_that_repeats_table_names_keeps_singular_classes():
    schema = Schema(
        tables={
            "customers": _table("customers"),
            "funds": _table("funds"),
            "orders": _table(
                "orders",
                "customers_id",
                "funds_id",
                fks=[_fk("customers_id", "customers"), _fk("funds_id", "funds")],
            ),
            "order_items": _table("order_items", "orders_id", fks=[_fk("orders_id", "orders")]),
        }
    )
    names, evidence = entity_names_for_mapping(_mapping(schema), schema)
    assert (names["customers"], names["orders"], names["order_items"]) == ("Customer", "Order", "OrderItem")
    assert names["funds"] == "Fund" and evidence == []


def test_names_do_not_depend_on_unrelated_tables():
    alone = _bases("basis_id")
    with_audit = _bases(
        "basis_id",
        more=[_table("customers"), _table("audit_log", "customers_id", fks=[_fk("customers_id", "customers")])],
    )
    assert entity_names_for_mapping(_mapping(alone), alone)[0]["bases"] == "Basis"
    assert entity_names_for_mapping(_mapping(with_audit), with_audit)[0]["bases"] == "Basis"


def test_role_names_are_not_evidence():
    # ship_via / reports_to / supplier_basis_id name a role, not the table.
    schema = Schema(
        tables={
            "bases": _table("bases"),
            "plans": _table(
                "plans", "ship_via", "reports_to", fks=[_fk("ship_via", "bases"), _fk("reports_to", "bases")]
            ),
            "lines": _table("lines", "supplier_basis_id", fks=[_fk("supplier_basis_id", "bases")]),
        }
    )
    names, evidence = entity_names_for_mapping(_mapping(schema), schema)
    assert names["bases"] == "Base" and evidence == []


def test_disagreeing_columns_leave_the_singularizer_in_charge():
    schema = _bases("basis_id", more=[_table("lines", "base_id", fks=[_fk("base_id", "bases")])])
    names, evidence = entity_names_for_mapping(_mapping(schema), schema)
    assert names["bases"] == "Base" and evidence == []


def test_inferred_keys_are_not_independent_evidence():
    # infer-fks matched them using the singularizer; counting them would be circular.
    schema = _bases("basis_id", constraint="inferred:plans_basis_id_fk")
    names, evidence = entity_names_for_mapping(_mapping(schema), schema)
    assert names["bases"] == "Base" and evidence == []


def test_a_renamed_collection_is_the_curators_choice():
    schema = _bases("basis_id")
    names, evidence = entity_names_for_mapping(_mapping(schema, bases="foundations"), schema)
    assert names["foundations"] == "Foundation" and evidence == []


def test_compound_names_keep_their_head_words():
    schema = Schema(
        tables={
            "cost_bases": _table("cost_bases"),
            "plans": _table("plans", "cost_basis_id", fks=[_fk("cost_basis_id", "cost_bases")]),
        }
    )
    names, _ = entity_names_for_mapping(_mapping(schema), schema)
    assert names["cost_bases"] == "CostBasis"


def test_a_collection_two_tables_share_gets_the_singularizer_name_in_any_order():
    schema = _bases("basis_id", more=[_table("bases_archive")])
    for order in (["bases", "bases_archive"], ["bases_archive", "bases"]):
        config = MappingConfig(
            collections={
                **{t: CollectionMapping(source_table=t, target_collection="bases") for t in order},
                "plans": CollectionMapping(source_table="plans", target_collection="plans"),
            },
            edges=[],
        )
        names, evidence = entity_names_for_mapping(config, schema)
        assert names["bases"] == "Base" and evidence == []


def test_csi_and_r2rml_name_the_class_the_same_way():
    schema = _bases("basis_id")
    config = _mapping(schema)
    doc = mapping_to_csi(config, schema)
    assert "Basis" in {e["name"] for e in doc["conceptualModel"]["entities"]}
    assert doc["provenance"]["entityNaming"][0]["name"] == "Basis"
    ttl = mapping_to_r2rml(config, schema)
    assert "concept#Basis>" in ttl and "concept#Base>" not in ttl


def test_no_evidence_means_no_provenance_entry():
    schema = _bases("base_id")
    doc = mapping_to_csi(_mapping(schema), schema)
    assert "entityNaming" not in doc["provenance"]


def test_curated_labels_key_on_the_evidence_name():
    # A curator's entry names the class r2g generates, so it must see "Basis".
    schema = _bases("basis_id")
    doc = mapping_to_csi(
        _mapping(schema),
        schema,
        curated_labels={"curatedLabelsVersion": "1", "entities": [{"name": "Basis", "label": "Foundation"}]},
    )
    assert doc["provenance"]["curatedLabels"][0]["resolution"] == "applied"
    assert "Foundation" in {e["name"] for e in doc["conceptualModel"]["entities"]}


def test_a_curated_identifier_names_a_table_the_keys_cannot():
    # The way to get Goods: the schema's goods_id is no evidence, a curator's word is.
    schema = Schema(
        tables={
            "goods": _table("goods"),
            "orders": _table("orders", "goods_id", fks=[_fk("goods_id", "goods")]),
        }
    )
    doc = mapping_to_csi(
        _mapping(schema),
        schema,
        curated_labels={
            "curatedLabelsVersion": "1",
            "entities": [{"name": "Good", "label": "Goods", "identifier": "Goods"}],
        },
    )
    assert doc["provenance"]["curatedLabels"][0]["resolution"] == "applied"
    assert "Goods" in {e["name"] for e in doc["conceptualModel"]["entities"]}
