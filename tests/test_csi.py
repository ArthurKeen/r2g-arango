"""Tests for the forward CSI v1 emitter (src/r2g/csi.py)."""

from __future__ import annotations

import sys

import pytest

from r2g.csi import CSI_VERSION, csi_schema, mapping_to_csi, validate_csi
from r2g.types import (
    CollectionMapping,
    Column,
    EdgeDefinition,
    FieldExpression,
    MappingConfig,
    Schema,
    Table,
)


@pytest.fixture(autouse=True)
def _reset_structlog():
    """Restore structlog to real stderr after a CliRunner invocation.

    CliRunner redirects stdout/stderr; structlog caches the (now-closed) stream,
    which poisons logging in every later test. Mirrors the fixture in
    tests/test_cli.py.
    """
    yield
    import structlog

    structlog.reset_defaults()
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(0),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )


def _sample_config() -> MappingConfig:
    """A two-table (users, orders) + one FK-edge mapping."""
    return MappingConfig(
        source_schema="shop",
        collections={
            "users": CollectionMapping(
                source_table="users",
                target_collection="User",
                field_mappings={"full_name": "name"},
            ),
            "orders": CollectionMapping(
                source_table="orders",
                target_collection="Order",
                field_expressions=[
                    FieldExpression(target="total_cents", sources=["total"], expression="total * 100"),
                ],
            ),
            # A join table -> becomes a relationship, not an entity.
            "user_orders": CollectionMapping(
                source_table="user_orders",
                target_collection="placed",
                collection_type="edge",
                is_join_table=True,
            ),
        },
        edges=[
            EdgeDefinition(
                edge_collection="placed_by",
                from_collection="orders",  # source-table name
                to_collection="users",  # source-table name
                from_fields=["user_id"],
                to_fields=["id"],
            ),
        ],
    )


def _sample_schema() -> Schema:
    return Schema(
        tables={
            "users": Table(
                name="users",
                columns=[
                    Column(name="id", data_type="integer", is_primary_key=True),
                    Column(name="full_name", data_type="text"),
                    Column(name="email", data_type="text"),
                ],
                primary_key=["id"],
            ),
            "orders": Table(
                name="orders",
                columns=[
                    Column(name="id", data_type="integer", is_primary_key=True),
                    Column(name="user_id", data_type="integer"),
                    Column(name="total", data_type="numeric"),
                ],
                primary_key=["id"],
            ),
        }
    )


def test_emits_valid_csi_without_schema():
    doc = mapping_to_csi(_sample_config(), source_type="postgresql")
    validate_csi(doc)  # raises if invalid
    assert doc["csiVersion"] == CSI_VERSION == "1"


def test_emits_valid_csi_with_schema():
    doc = mapping_to_csi(_sample_config(), _sample_schema(), source_type="postgresql")
    validate_csi(doc)


def test_entities_are_document_collections_only():
    doc = mapping_to_csi(_sample_config())
    names = {e["name"] for e in doc["conceptualModel"]["entities"]}
    # Join table 'placed' must NOT appear as an entity.
    assert names == {"User", "Order"}
    assert set(doc["arangoPhysicalMapping"]["entities"]) == {"User", "Order"}
    for phys in doc["arangoPhysicalMapping"]["entities"].values():
        assert phys["style"] == "COLLECTION"


def test_relationship_endpoints_resolve_to_target_collections():
    doc = mapping_to_csi(_sample_config())
    rels = doc["conceptualModel"]["relationships"]
    assert len(rels) == 1
    rel = rels[0]
    assert rel["type"] == "placedBy"  # CC-12 lowerCamel
    # from_collection='orders' -> 'Order', to_collection='users' -> 'User'.
    assert rel["fromEntity"] == "Order"
    assert rel["toEntity"] == "User"


def test_physical_relationships_omit_collection_name():
    doc = mapping_to_csi(_sample_config())
    phys = doc["arangoPhysicalMapping"]["relationships"]["placedBy"]
    assert phys["style"] == "DEDICATED_COLLECTION"
    assert phys["edgeCollectionName"] == "placed_by"
    # CSI schema forbids collectionName on relationships.
    assert "collectionName" not in phys


def test_properties_prefer_mapping_then_columns():
    doc = mapping_to_csi(_sample_config(), _sample_schema())
    entities = {e["name"]: e for e in doc["conceptualModel"]["entities"]}
    user_props = [p["name"] for p in entities["User"]["properties"]]
    # Renamed 'full_name' -> 'name' comes first; unmapped columns follow (as-is).
    assert user_props[0] == "name"
    assert "email" in user_props
    assert "id" in user_props
    # The renamed source column 'full_name' must not leak through as itself.
    assert "full_name" not in user_props


def test_properties_without_schema_use_explicit_mappings_only():
    doc = mapping_to_csi(_sample_config())
    entities = {e["name"]: e for e in doc["conceptualModel"]["entities"]}
    assert [p["name"] for p in entities["User"]["properties"]] == ["name"]
    assert [p["name"] for p in entities["Order"]["properties"]] == ["totalCents"]  # CC-12 lowerCamel


def test_provenance_shape():
    doc = mapping_to_csi(
        _sample_config(),
        source_type="mysql",
        source_ref="shopdb",
        producer_version="9.9.9",
        generated_at="2026-07-14T00:00:00+00:00",
        confidence=1.0,
    )
    prov = doc["provenance"]
    assert prov["producer"] == "r2g"
    assert prov["producerVersion"] == "9.9.9"
    assert prov["direction"] == "forward"
    assert prov["source"] == {"kind": "mysql", "ref": "shopdb", "fingerprint": None}
    assert prov["generatedAt"] == "2026-07-14T00:00:00+00:00"
    assert prov["confidence"] == 1.0


def test_source_ref_defaults_to_source_schema():
    doc = mapping_to_csi(_sample_config())
    assert doc["provenance"]["source"]["ref"] == "shop"
    assert doc["provenance"]["source"]["kind"] == "relational"


def test_producer_version_defaults_to_installed():
    from r2g import __version__

    doc = mapping_to_csi(_sample_config())
    assert doc["provenance"]["producerVersion"] == __version__


def test_confidence_omitted_by_default():
    doc = mapping_to_csi(_sample_config())
    assert "confidence" not in doc["provenance"]


def test_emitter_is_deterministic():
    cfg = _sample_config()
    assert mapping_to_csi(cfg) == mapping_to_csi(cfg)


def test_csi_schema_loads():
    schema = csi_schema()
    assert schema["required"] == [
        "csiVersion",
        "conceptualModel",
        "arangoPhysicalMapping",
        "provenance",
    ]


def test_invalid_document_rejected():
    import jsonschema

    with pytest.raises(jsonschema.ValidationError):
        validate_csi({"csiVersion": "1"})  # missing required blocks


def test_export_csi_cli(tmp_path):
    import json

    from typer.testing import CliRunner

    from r2g.main import app

    config_path = tmp_path / "mapping.yaml"
    config_path.write_text(
        "source_schema: shop\n"
        "collections:\n"
        "  users:\n"
        "    source_table: users\n"
        "    target_collection: User\n"
        "    field_mappings:\n"
        "      full_name: name\n"
        "  orders:\n"
        "    source_table: orders\n"
        "    target_collection: Order\n"
        "edges:\n"
        "  - edge_collection: placed_by\n"
        "    from_collection: orders\n"
        "    to_collection: users\n"
        "    from_field: user_id\n"
        "    to_field: id\n",
        encoding="utf-8",
    )
    out = tmp_path / "out.csi.json"
    result = CliRunner().invoke(
        app,
        ["export-csi", "--config", str(config_path), "--source-type", "postgresql", "-o", str(out)],
    )
    assert result.exit_code == 0, result.output
    doc = json.loads(out.read_text(encoding="utf-8"))
    validate_csi(doc)
    assert doc["provenance"]["source"]["kind"] == "postgresql"
    assert {e["name"] for e in doc["conceptualModel"]["entities"]} == {"User", "Order"}


# ── Attribute-label collisions ───────────────────────────────────────
#
# Two entities in ONE document emitting the same attribute label make that word
# ambiguous for any consumer deriving a flat vocabulary from it, and the
# distinction cannot be recovered downstream because it was discarded here.


def _colliding_config(**overrides) -> MappingConfig:
    """Contract + Opportunity, both carrying renewal_date / product_scope."""
    return MappingConfig(
        source_schema="crm",
        collections={
            "contracts": CollectionMapping(
                source_table="contracts", target_collection="Contract"
            ),
            "opportunities": CollectionMapping(
                source_table="opportunities", target_collection="Opportunity"
            ),
        },
        **overrides,
    )


def _colliding_schema() -> Schema:
    shared = ["renewal_date", "product_scope"]
    return Schema(
        tables={
            "contracts": Table(
                name="contracts",
                columns=[Column(name="id", data_type="integer", is_primary_key=True)]
                + [Column(name=c, data_type="text") for c in shared]
                + [Column(name="auto_renew", data_type="boolean")],
                primary_key=["id"],
            ),
            "opportunities": Table(
                name="opportunities",
                columns=[Column(name="id", data_type="integer", is_primary_key=True)]
                + [Column(name=c, data_type="text") for c in shared]
                + [Column(name="amount_usd", data_type="numeric")],
                primary_key=["id"],
            ),
        }
    )


def _labels_by_entity(doc):
    return {e["name"]: [p["name"] for p in e["properties"]] for e in doc["conceptualModel"]["entities"]}


def _all_labels(doc):
    out = []
    for e in doc["conceptualModel"]["entities"]:
        out += [p["name"] for p in e["properties"]]
    return out


class TestLabelCollisions:
    def test_default_policy_qualifies_every_occurrence(self):
        doc = mapping_to_csi(_colliding_config(), _colliding_schema())
        labels = _labels_by_entity(doc)
        assert "contractRenewalDate" in labels["Contract"]
        assert "opportunityRenewalDate" in labels["Opportunity"]
        # Neither entity keeps the bare label: leaving one behind would preserve
        # exactly the false confidence this fixes.
        assert "renewalDate" not in _all_labels(doc)

    def test_no_duplicate_labels_remain(self):
        doc = mapping_to_csi(_colliding_config(), _colliding_schema())
        labels = _all_labels(doc)
        dupes = {n for n in labels if labels.count(n) > 1}
        # Nothing survives here: 'id' qualifies cleanly to contractId /
        # opportunityId because neither name is otherwise taken. (When one IS
        # taken the group is refused instead — see the cascade test.)
        assert dupes == set()
        assert {"contractId", "opportunityId"} <= set(labels)

    def test_physical_mapping_still_resolves_to_the_source_column(self):
        """The rename is conceptual only — the stored field must not move."""
        doc = mapping_to_csi(_colliding_config(), _colliding_schema())
        phys = doc["arangoPhysicalMapping"]["entities"]
        assert phys["Contract"]["properties"]["contractRenewalDate"]["field"] == "renewal_date"
        assert phys["Opportunity"]["properties"]["opportunityRenewalDate"]["field"] == "renewal_date"
        assert phys["Contract"]["collectionName"] == "Contract"

    def test_every_conceptual_property_has_a_physical_field(self):
        doc = mapping_to_csi(_colliding_config(), _colliding_schema())
        phys = doc["arangoPhysicalMapping"]["entities"]
        for entity in doc["conceptualModel"]["entities"]:
            mapped = phys[entity["name"]]["properties"]
            for prop in entity["properties"]:
                assert prop["name"] in mapped, (entity["name"], prop["name"])

    def test_collisions_are_recorded_in_provenance(self):
        doc = mapping_to_csi(_colliding_config(), _colliding_schema())
        recs = {r["label"]: r for r in doc["provenance"]["labelCollisions"]}
        assert recs["renewalDate"]["resolution"] == "qualified"
        assert recs["renewalDate"]["entities"] == ["Contract", "Opportunity"]
        assert recs["renewalDate"]["renamedTo"] == {
            "Contract": "contractRenewalDate",
            "Opportunity": "opportunityRenewalDate",
        }

    def test_warn_policy_records_without_renaming(self):
        doc = mapping_to_csi(_colliding_config(), _colliding_schema(), label_policy="warn")
        assert "renewalDate" in _labels_by_entity(doc)["Contract"]
        assert "renewalDate" in _labels_by_entity(doc)["Opportunity"]
        recs = {r["label"]: r for r in doc["provenance"]["labelCollisions"]}
        assert recs["renewalDate"]["resolution"] == "reported"
        assert "renamedTo" not in recs["renewalDate"]

    def test_off_policy_skips_the_check_entirely(self):
        doc = mapping_to_csi(_colliding_config(), _colliding_schema(), label_policy="off")
        assert "renewalDate" in _labels_by_entity(doc)["Contract"]
        assert "labelCollisions" not in doc["provenance"]

    def test_qualification_that_would_create_a_new_collision_is_refused(self):
        """``Account.id`` must not become ``accountId`` when that is taken.

        Trading one ambiguity for another is worse than reporting it, because
        the new one looks deliberate.
        """
        config = MappingConfig(
            collections={
                "accounts": CollectionMapping(source_table="accounts", target_collection="Account"),
                "contacts": CollectionMapping(source_table="contacts", target_collection="Contact"),
            }
        )
        schema = Schema(
            tables={
                "accounts": Table(
                    name="accounts",
                    columns=[
                        Column(name="id", data_type="integer", is_primary_key=True),
                        # The business key already owns the qualified form.
                        Column(name="account_id", data_type="text"),
                    ],
                    primary_key=["id"],
                ),
                "contacts": Table(
                    name="contacts",
                    columns=[Column(name="id", data_type="integer", is_primary_key=True)],
                    primary_key=["id"],
                ),
            }
        )
        doc = mapping_to_csi(config, schema)
        rec = next(r for r in doc["provenance"]["labelCollisions"] if r["label"] == "id")
        assert rec["resolution"] == "unresolved"
        assert "accountId" in rec["reason"]
        # Left untouched rather than mangled.
        assert _labels_by_entity(doc)["Account"].count("id") == 1

    def test_collision_free_document_records_nothing(self):
        """Documents with no shared label keep their historical bytes."""
        config = MappingConfig(
            collections={
                "books": CollectionMapping(source_table="books", target_collection="Book"),
                "shelves": CollectionMapping(source_table="shelves", target_collection="Shelf"),
            }
        )
        schema = Schema(
            tables={
                "books": Table(
                    name="books", columns=[Column(name="isbn", data_type="text")], primary_key=[]
                ),
                "shelves": Table(
                    name="shelves", columns=[Column(name="aisle", data_type="text")], primary_key=[]
                ),
            }
        )
        doc = mapping_to_csi(config, schema)
        assert "labelCollisions" not in doc["provenance"]

    def test_output_is_independent_of_collection_insertion_order(self):
        forward = mapping_to_csi(_colliding_config(), _colliding_schema())
        reversed_config = MappingConfig(
            source_schema="crm",
            collections={
                "opportunities": CollectionMapping(
                    source_table="opportunities", target_collection="Opportunity"
                ),
                "contracts": CollectionMapping(
                    source_table="contracts", target_collection="Contract"
                ),
            },
        )
        backward = mapping_to_csi(reversed_config, _colliding_schema())
        assert _labels_by_entity(forward) == _labels_by_entity(backward)
        assert forward["provenance"]["labelCollisions"] == backward["provenance"]["labelCollisions"]

    def test_document_still_validates_against_the_csi_schema(self):
        doc = mapping_to_csi(_colliding_config(), _colliding_schema(), source_type="postgresql")
        validate_csi(doc)

    def test_contested_qualified_name_is_resolved_deterministically(self):
        """Two collision groups can want the SAME qualified name.

        ``User.nameId`` and ``UserName.id`` both qualify to ``userNameId``.
        Which group wins must not depend on mapping insertion order, so groups
        are processed in sorted label order: ``id`` claims it, ``nameId`` is
        then refused rather than silently overwriting it.
        """
        tables = {
            "users": Table(
                name="users",
                columns=[
                    Column(name="id", data_type="integer", is_primary_key=True),
                    Column(name="name_id", data_type="integer"),
                ],
                primary_key=["id"],
            ),
            "user_names": Table(
                name="user_names",
                columns=[
                    Column(name="id", data_type="integer", is_primary_key=True),
                    Column(name="label", data_type="text"),
                ],
                primary_key=["id"],
            ),
            "audits": Table(
                name="audits",
                columns=[
                    Column(name="name_id", data_type="integer"),
                    Column(name="note", data_type="text"),
                ],
                primary_key=[],
            ),
        }
        names = {"users": "User", "user_names": "UserName", "audits": "Audit"}

        def build(order):
            return mapping_to_csi(
                MappingConfig(
                    collections={
                        t: CollectionMapping(source_table=t, target_collection=names[t])
                        for t in order
                    }
                ),
                Schema(tables=tables),
            )

        forward = build(["users", "user_names", "audits"])
        backward = build(["audits", "user_names", "users"])

        recs = {r["label"]: r for r in forward["provenance"]["labelCollisions"]}
        assert recs["id"]["resolution"] == "qualified"
        assert recs["id"]["renamedTo"]["UserName"] == "userNameId"
        assert recs["nameId"]["resolution"] == "unresolved"
        assert "userNameId" in recs["nameId"]["reason"]

        # The contested name is claimed by exactly one entity, either way round.
        assert _labels_by_entity(forward) == _labels_by_entity(backward)
        assert _all_labels(forward).count("userNameId") == 1


class TestRolesPolicy:
    """`--label-policy roles`: classify a collision, then fit the remedy to it.

    A collision on a plain column (`renewalDate`) and one on a foreign key
    (`accountId`) are not the same problem, and qualifying both alike produces
    `accountAccountId` — a name that reads as a business attribute when the
    thing it describes is a join.
    """

    def _crm(self):
        """Account owns account_id; three others reference it. All share `id`."""
        config = MappingConfig(
            collections={
                "accounts": CollectionMapping(source_table="accounts", target_collection="Account"),
                "contacts": CollectionMapping(source_table="contacts", target_collection="Contact"),
                "contracts": CollectionMapping(source_table="contracts", target_collection="Contract"),
            },
            edges=[
                EdgeDefinition(
                    edge_collection="contacts_of_account",
                    from_collection="contacts", to_collection="accounts",
                    from_fields=["account_id"], to_fields=["account_id"],
                ),
                EdgeDefinition(
                    edge_collection="contracts_of_account",
                    from_collection="contracts", to_collection="accounts",
                    from_fields=["account_id"], to_fields=["account_id"],
                ),
            ],
        )
        schema = Schema(
            tables={
                "accounts": Table(
                    name="accounts",
                    columns=[
                        Column(name="id", data_type="integer", is_primary_key=True),
                        Column(name="account_id", data_type="text"),
                        Column(name="account_name", data_type="text"),
                    ],
                    primary_key=["id"],
                ),
                "contacts": Table(
                    name="contacts",
                    columns=[
                        Column(name="id", data_type="integer", is_primary_key=True),
                        Column(name="account_id", data_type="text"),
                        # A single-owner business key that blocks `id` from
                        # qualifying to `contactId` — exactly the situation in
                        # the real CRM catalog.
                        Column(name="contact_id", data_type="text"),
                        Column(name="renewal_date", data_type="date"),
                    ],
                    primary_key=["id"],
                ),
                "contracts": Table(
                    name="contracts",
                    columns=[
                        Column(name="id", data_type="integer", is_primary_key=True),
                        Column(name="account_id", data_type="text"),
                        Column(name="renewal_date", data_type="date"),
                    ],
                    primary_key=["id"],
                ),
            }
        )
        return config, schema

    def test_foreign_key_owner_keeps_the_bare_label(self):
        doc = mapping_to_csi(*self._crm(), label_policy="roles")
        labels = _labels_by_entity(doc)
        assert "accountId" in labels["Account"]
        assert "contactAccountId" in labels["Contact"]
        assert "contractAccountId" in labels["Contract"]
        # The stutter this policy exists to prevent.
        assert "accountAccountId" not in _all_labels(doc)

    def test_primary_key_becomes_identity_not_an_attribute(self):
        doc = mapping_to_csi(*self._crm(), label_policy="roles")
        assert "id" not in _all_labels(doc)
        rec = next(r for r in doc["provenance"]["labelCollisions"] if r["label"] == "id")
        assert rec["resolution"] == "identity"
        assert rec["kind"] == "structural"
        # Dropped from the physical mapping too, so the two stay in step.
        assert "id" not in doc["arangoPhysicalMapping"]["entities"]["Account"]["properties"]

    def test_semantic_collision_still_qualifies_every_occurrence(self):
        doc = mapping_to_csi(*self._crm(), label_policy="roles")
        labels = _labels_by_entity(doc)
        assert "contactRenewalDate" in labels["Contact"]
        assert "contractRenewalDate" in labels["Contract"]
        assert "renewalDate" not in _all_labels(doc)
        rec = next(r for r in doc["provenance"]["labelCollisions"] if r["label"] == "renewalDate")
        assert rec["kind"] == "semantic"

    def test_roles_leaves_no_duplicate_labels_where_qualify_does(self):
        config, schema = self._crm()
        q = _all_labels(mapping_to_csi(config, schema, label_policy="qualify"))
        r = _all_labels(mapping_to_csi(config, schema, label_policy="roles"))
        assert {n for n in q if q.count(n) > 1} == {"id"}  # qualify cannot fix it
        assert {n for n in r if r.count(n) > 1} == set()

    def test_owner_is_recorded(self):
        doc = mapping_to_csi(*self._crm(), label_policy="roles")
        rec = next(r for r in doc["provenance"]["labelCollisions"] if r["label"] == "accountId")
        assert rec["owner"] == "Account"
        assert rec["kind"] == "structural"
        assert "Account" not in rec["renamedTo"]

    def test_role_is_read_through_field_mappings(self):
        """A renamed key column must still classify as a key.

        The physical `field` is the *stored* attribute, which diverges from the
        source column whenever field_mappings renames one (pagila stores
        `actorId` for `actor_id`). Classifying off `field` would silently mark
        every renamed key as a plain attribute.
        """
        config = MappingConfig(
            collections={
                "actors": CollectionMapping(
                    source_table="actors", target_collection="Actor",
                    field_mappings={"actor_id": "actorId"},
                ),
                "film_actors": CollectionMapping(
                    source_table="film_actors", target_collection="FilmActor",
                    field_mappings={"actor_id": "actorId"},
                ),
            }
        )
        schema = Schema(
            tables={
                "actors": Table(
                    name="actors",
                    columns=[Column(name="actor_id", data_type="integer", is_primary_key=True)],
                    primary_key=["actor_id"],
                ),
                "film_actors": Table(
                    name="film_actors",
                    columns=[
                        Column(name="actor_id", data_type="integer", is_primary_key=True),
                        Column(name="film_id", data_type="integer", is_primary_key=True),
                    ],
                    primary_key=["actor_id", "film_id"],
                ),
            }
        )
        doc = mapping_to_csi(config, schema, label_policy="roles")
        rec = next(r for r in doc["provenance"]["labelCollisions"] if r["label"] == "actorId")
        assert rec["kind"] == "structural", "renamed key column misclassified as semantic"

    def test_seam_word_is_not_repeated(self):
        """`FilmCategory` + `categoryId` must not become `filmCategoryCategoryId`."""
        config = MappingConfig(
            collections={
                "categories": CollectionMapping(
                    source_table="categories", target_collection="Category"
                ),
                "film_categories": CollectionMapping(
                    source_table="film_categories", target_collection="FilmCategory"
                ),
            }
        )
        schema = Schema(
            tables={
                "categories": Table(
                    name="categories",
                    columns=[Column(name="category_id", data_type="integer", is_primary_key=True)],
                    primary_key=["category_id"],
                ),
                "film_categories": Table(
                    name="film_categories",
                    columns=[
                        Column(name="category_id", data_type="integer"),
                        Column(name="note", data_type="text"),
                    ],
                    primary_key=[],
                ),
            }
        )
        doc = mapping_to_csi(config, schema, label_policy="roles")
        labels = _all_labels(doc)
        assert "filmCategoryCategoryId" not in labels
        assert "filmCategoryId" in labels
        assert "categoryId" in _labels_by_entity(doc)["Category"]

    def test_physical_fields_are_never_invented_or_moved(self):
        config, schema = self._crm()
        base = mapping_to_csi(config, schema, label_policy="off")
        doc = mapping_to_csi(config, schema, label_policy="roles")
        for entity in doc["conceptualModel"]["entities"]:
            name = entity["name"]
            after = doc["arangoPhysicalMapping"]["entities"][name]["properties"]
            before = base["arangoPhysicalMapping"]["entities"][name]["properties"]
            assert {v["field"] for v in after.values()} <= {v["field"] for v in before.values()}
            # Conceptual and physical property sets stay in step.
            assert {p["name"] for p in entity["properties"]} == set(after)

    def test_document_still_validates(self):
        doc = mapping_to_csi(*self._crm(), source_type="postgresql", label_policy="roles")
        validate_csi(doc)

    def test_qualify_policy_is_unchanged_by_the_new_code(self):
        """The historical default must stay byte-identical and stutter-free-free."""
        doc = mapping_to_csi(*self._crm(), label_policy="qualify")
        assert "accountAccountId" in _all_labels(doc)  # blunt, as documented
        rec = next(r for r in doc["provenance"]["labelCollisions"] if r["label"] == "accountId")
        assert "kind" not in rec and "owner" not in rec

    def test_owner_found_by_primary_key_when_the_name_does_not_match(self):
        """The PK tier of owner selection, isolated.

        `ownerId` lives on a table called `people`, so the name-match tier finds
        nothing; only "who holds it as a primary key" identifies the owner. If
        that tier is skipped, Person gets qualified too and loses the bare label.
        """
        config = MappingConfig(
            collections={
                "people": CollectionMapping(source_table="people", target_collection="Person"),
                "assets": CollectionMapping(source_table="assets", target_collection="Asset"),
            },
            edges=[
                EdgeDefinition(
                    edge_collection="assets_of_person",
                    from_collection="assets", to_collection="people",
                    from_fields=["owner_id"], to_fields=["owner_id"],
                ),
            ],
        )
        schema = Schema(
            tables={
                "people": Table(
                    name="people",
                    columns=[
                        Column(name="owner_id", data_type="integer", is_primary_key=True),
                        Column(name="full_name", data_type="text"),
                    ],
                    primary_key=["owner_id"],
                ),
                "assets": Table(
                    name="assets",
                    columns=[
                        Column(name="owner_id", data_type="integer"),
                        Column(name="tag", data_type="text"),
                    ],
                    primary_key=[],
                ),
            }
        )
        doc = mapping_to_csi(config, schema, label_policy="roles")
        rec = next(r for r in doc["provenance"]["labelCollisions"] if r["label"] == "ownerId")
        assert rec["owner"] == "Person"
        assert "ownerId" in _labels_by_entity(doc)["Person"]
        assert "assetOwnerId" in _labels_by_entity(doc)["Asset"]
