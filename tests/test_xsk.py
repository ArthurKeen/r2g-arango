"""Cross-source shared-key inference (PRD P6.7).

The motivating case is the ClickHouse connector (WP-CH2): ``account_id`` is
shared by ``clickhouse.query_events`` / ``usage_metrics`` but the ``Account``
entity lives in the PostgreSQL CRM source, so single-schema FK inference (P6.6)
returns zero candidates no matter how low the threshold goes.

Every guard here is tested in BOTH directions — a suppression test is paired
with a case that must still produce a candidate — so removing a guard makes a
test fail rather than merely widening the output.
"""

from __future__ import annotations

import pytest

from r2g.config import pg_type_to_json_type
from r2g.csi import mapping_to_csi, validate_csi
from r2g.types import (
    CollectionMapping,
    Column,
    MappingConfig,
    Schema,
    SharedKey,
    SharedKeyBinding,
    Table,
)
from r2g.xsk import (
    SharedKeyOptions,
    concept_name,
    entity_of,
    infer_shared_keys,
)

# ── Fixtures ─────────────────────────────────────────────────────────


def col(name: str, data_type: str = "text", *, pk: bool = False, nullable: bool = True) -> Column:
    return Column(name=name, data_type=data_type, is_nullable=nullable, is_primary_key=pk)


def table(name: str, columns: list[Column], pk: list[str] | None = None) -> Table:
    return Table(name=name, columns=columns, primary_key=pk or [], foreign_keys=[])


def schema(*tables: Table) -> Schema:
    return Schema(tables={t.name: t for t in tables})


@pytest.fixture
def clickhouse_schema() -> Schema:
    """The real shape of the WP-CH2 demo source (ClickHouse types, no FKs)."""
    return schema(
        table(
            "query_events",
            [
                col("event_id", "uint64", pk=True, nullable=False),
                col("account_id", "string"),
                col("feature", "string"),
                col("query_count", "uint64"),
            ],
            pk=["event_id"],
        ),
        table(
            "usage_metrics",
            [
                col("id", "uint64", pk=True, nullable=False),
                col("account_id", "string"),
                col("edition", "string"),
                col("graphrag_enabled", "uint8"),
            ],
            pk=["id"],
        ),
    )


@pytest.fixture
def crm_schema() -> Schema:
    """A CRM source that owns the Account entity (PK ``id``, business key ``account_id``)."""
    return schema(
        table(
            "accounts",
            [
                col("id", "integer", pk=True, nullable=False),
                col("account_id", "character varying", nullable=False),
                col("account_name", "text"),
            ],
            pk=["id"],
        ),
        table(
            "contracts",
            [
                col("id", "integer", pk=True, nullable=False),
                col("account_id", "character varying"),
                col("contract_id", "character varying"),
            ],
            pk=["id"],
        ),
    )


# ── The motivating case ──────────────────────────────────────────────


class TestMotivatingCase:
    def test_finds_the_cross_source_account_hub(self, clickhouse_schema, crm_schema):
        result = infer_shared_keys(
            {"clickhouse_analytics": clickhouse_schema, "crm": crm_schema},
            options=SharedKeyOptions(min_confidence=0.0),
        )

        keys = [c.key for c in result.candidates]
        assert "account_id" in keys

        cand = next(c for c in result.candidates if c.key == "account_id")
        assert cand.hub.kind == "entity"
        assert cand.hub.concept == "Account"
        assert (cand.hub.source, cand.hub.table) == ("crm", "accounts")

        # Exactly the PRD's stated example: query_events -> Account,
        # usage_metrics -> Account. The CRM's own tables are NOT references.
        assert sorted((r.source, r.table) for r in cand.references) == [
            ("clickhouse_analytics", "query_events"),
            ("clickhouse_analytics", "usage_metrics"),
        ]

    def test_single_schema_inference_finds_nothing_on_the_same_input(self, clickhouse_schema):
        """Pins *why* P6.7 exists — if this ever fails, P6.6 grew the capability."""
        from r2g.fk_inference import InferenceOptions, infer_foreign_keys

        candidates = infer_foreign_keys(
            clickhouse_schema, options=InferenceOptions(min_confidence=0.0)
        )
        assert candidates == []


# ── Guards, each tested in both directions ───────────────────────────


class TestSourceSpanGuard:
    def test_single_source_key_is_suppressed_with_a_reason(self, crm_schema):
        # ``contract_id`` exists only in the CRM source.
        result = infer_shared_keys(
            {"crm": crm_schema, "other": schema(table("t", [col("x", "text")]))},
            options=SharedKeyOptions(min_confidence=0.0),
        )
        assert "contract_id" not in [c.key for c in result.candidates]
        reasons = {
            (s.key, s.reason) for s in result.suppressed
        }
        assert ("contract_id", "single_source") in reasons

    def test_same_key_in_two_sources_does_produce_a_candidate(self, crm_schema):
        """The paired direction: the guard must be about source span, nothing else."""
        second = schema(table("tickets", [col("contract_id", "character varying")]))
        result = infer_shared_keys(
            {"crm": crm_schema, "support": second},
            options=SharedKeyOptions(min_confidence=0.0),
        )
        assert "contract_id" in [c.key for c in result.candidates]


class TestGenericNameGuard:
    def test_bare_id_never_groups_across_sources(self):
        a = schema(table("t1", [col("id", "integer", pk=True)], pk=["id"]))
        b = schema(table("t2", [col("id", "integer", pk=True)], pk=["id"]))
        result = infer_shared_keys({"a": a, "b": b}, options=SharedKeyOptions(min_confidence=0.0))
        assert result.candidates == []
        assert any(s.reason == "generic_name" for s in result.suppressed)

    def test_prefixed_id_does_group(self):
        a = schema(table("t1", [col("tenant_id", "integer")]))
        b = schema(table("t2", [col("tenant_id", "integer")]))
        result = infer_shared_keys({"a": a, "b": b}, options=SharedKeyOptions(min_confidence=0.0))
        assert [c.key for c in result.candidates] == ["tenant_id"]


class TestTypeCompatibilityGuard:
    def test_incompatible_member_is_suppressed_not_silently_dropped(self):
        a = schema(table("ta", [col("order_id", "integer")]))
        b = schema(table("tb", [col("order_id", "integer")]))
        c = schema(table("tc", [col("order_id", "text")]))
        result = infer_shared_keys(
            {"a": a, "b": b, "c": c}, options=SharedKeyOptions(min_confidence=0.0)
        )
        cand = next(x for x in result.candidates if x.key == "order_id")
        assert cand.json_type == "integer"
        assert {(r.source, r.table) for r in cand.references} == {("a", "ta"), ("b", "tb")}

        mismatches = [s for s in result.suppressed if s.reason == "type_mismatch"]
        assert [(m.source, m.table) for m in mismatches] == [("c", "tc")]
        assert "integer" in mismatches[0].detail

    def test_clickhouse_and_postgres_integer_keys_are_compatible(self):
        """Regression: ``uint64`` used to fall through to ``string``."""
        assert pg_type_to_json_type("uint64") == "integer"
        assert pg_type_to_json_type("bigint") == "integer"
        a = schema(table("events", [col("tenant_id", "uint64")]))
        b = schema(table("tenants", [col("tenant_id", "bigint")]))
        result = infer_shared_keys({"ch": a, "pg": b}, options=SharedKeyOptions(min_confidence=0.0))
        cand = next(c for c in result.candidates if c.key == "tenant_id")
        assert cand.json_type == "integer"
        assert not [s for s in result.suppressed if s.reason == "type_mismatch"]


class TestHubSelection:
    def test_sole_primary_key_outranks_a_merely_named_table(self):
        owner = schema(
            table("account", [col("account_id", "text", pk=True, nullable=False)], pk=["account_id"])
        )
        user = schema(table("events", [col("account_id", "text")]))
        result = infer_shared_keys(
            {"owner": owner, "user": user}, options=SharedKeyOptions(min_confidence=0.0)
        )
        cand = result.candidates[0]
        assert cand.method == "hub_primary_key"
        assert cand.hub.is_primary_key is True
        assert cand.confidence >= 0.9

    def test_pk_hub_wins_when_a_DIFFERENT_table_matches_the_name(self):
        """Discriminating case for the preference order.

        ``test_sole_primary_key_outranks_a_merely_named_table`` cannot see the
        ordering at all, because there the one table is both name-matched and
        sole-PK. Here they are different tables in different sources, so
        swapping the preference order changes the answer.
        """
        warehouse = schema(
            table(
                "account_lookup",
                [col("account_id", "text", pk=True, nullable=False)],
                pk=["account_id"],
            )
        )
        crm = schema(table("accounts", [col("account_id", "text"), col("name", "text")]))
        app = schema(table("events", [col("account_id", "text")]))

        cand = infer_shared_keys(
            {"warehouse": warehouse, "crm": crm, "app": app},
            options=SharedKeyOptions(min_confidence=0.0),
        ).candidates[0]

        assert cand.method == "hub_primary_key"
        assert (cand.hub.source, cand.hub.table) == ("warehouse", "account_lookup")

    def test_named_table_hub_scores_below_a_pk_hub(self, clickhouse_schema, crm_schema):
        result = infer_shared_keys(
            {"clickhouse_analytics": clickhouse_schema, "crm": crm_schema},
            options=SharedKeyOptions(min_confidence=0.0),
        )
        cand = next(c for c in result.candidates if c.key == "account_id")
        assert cand.method == "hub_named_table"
        assert 0.6 <= cand.confidence < 0.9

    def test_virtual_hub_when_no_source_owns_the_key_and_no_table_is_invented(self):
        a = schema(table("clicks", [col("visitor_id", "text")]))
        b = schema(table("sessions", [col("visitor_id", "text")]))
        result = infer_shared_keys({"a": a, "b": b}, options=SharedKeyOptions(min_confidence=0.0))
        cand = result.candidates[0]
        assert cand.hub.kind == "virtual"
        assert cand.method == "virtual_hub"
        assert cand.hub.concept == "Visitor"
        # P6.7 forbids inventing a table for a key nobody owns.
        assert cand.hub.table is None and cand.hub.source is None
        assert {(r.source, r.table) for r in cand.references} == {("a", "clicks"), ("b", "sessions")}

    def test_hub_table_and_same_source_refs_are_reported(self, clickhouse_schema, crm_schema):
        result = infer_shared_keys(
            {"clickhouse_analytics": clickhouse_schema, "crm": crm_schema},
            options=SharedKeyOptions(min_confidence=0.0),
        )
        by_reason = {}
        for s in result.suppressed:
            by_reason.setdefault(s.reason, []).append((s.source, s.table))
        assert ("crm", "accounts") in by_reason["hub_table"]
        assert ("crm", "contracts") in by_reason["same_source_as_hub"]


class TestDeterminism:
    def test_repeated_runs_are_identical(self, clickhouse_schema, crm_schema):
        args = ({"clickhouse_analytics": clickhouse_schema, "crm": crm_schema},)
        opts = SharedKeyOptions(min_confidence=0.0)
        first = infer_shared_keys(*args, options=opts).model_dump()
        second = infer_shared_keys(*args, options=opts).model_dump()
        assert first == second

    def test_source_ordering_does_not_change_the_result(self, clickhouse_schema, crm_schema):
        opts = SharedKeyOptions(min_confidence=0.0)
        a = infer_shared_keys(
            {"clickhouse_analytics": clickhouse_schema, "crm": crm_schema}, options=opts
        ).model_dump()
        b = infer_shared_keys(
            {"crm": crm_schema, "clickhouse_analytics": clickhouse_schema}, options=opts
        ).model_dump()
        assert a == b


# ── Sampling ─────────────────────────────────────────────────────────


class _FakeSampler:
    """Stands in for an RSA value sampler (only the two probes P6.7 uses)."""

    def __init__(self, *, ratios=None, values=None):
        self._ratios = ratios or {}
        self._values = values or {}

    def distinct_ratio(self, table, column):
        return self._ratios.get((table, column))

    def sample_values(self, table, column, limit=1000):
        return self._values.get((table, column), [])


class TestSampling:
    def _schemas(self):
        return (
            {"hub": schema(table("accounts", [col("account_id", "text")]))},
            {"app": schema(table("events", [col("account_id", "text")]))},
        )

    def test_unique_hub_column_raises_confidence(self):
        hub, app = self._schemas()
        schemas = {**hub, **app}
        samplers = {"hub": _FakeSampler(ratios={("accounts", "account_id"): 1.0}), "app": None}
        with_sample = infer_shared_keys(
            schemas,
            options=SharedKeyOptions(min_confidence=0.0, sample_overlap=True),
            samplers=samplers,
        ).candidates[0]
        without = infer_shared_keys(
            schemas, options=SharedKeyOptions(min_confidence=0.0)
        ).candidates[0]
        assert with_sample.confidence > without.confidence
        assert any("unique in sample" in e for e in with_sample.evidence)

    def test_non_unique_hub_column_lowers_confidence(self):
        hub, app = self._schemas()
        samplers = {"hub": _FakeSampler(ratios={("accounts", "account_id"): 0.02}), "app": None}
        cand = infer_shared_keys(
            {**hub, **app},
            options=SharedKeyOptions(min_confidence=0.0, sample_overlap=True),
            samplers=samplers,
        ).candidates[0]
        assert cand.confidence < 0.7
        assert any("NOT unique" in e for e in cand.evidence)

    def test_high_value_overlap_is_a_boost(self):
        hub, app = self._schemas()
        samplers = {
            "hub": _FakeSampler(
                ratios={("accounts", "account_id"): 1.0},
                values={("accounts", "account_id"): ["a", "b", "c"]},
            ),
            "app": _FakeSampler(values={("events", "account_id"): ["a", "b", "c"]}),
        }
        cand = infer_shared_keys(
            {**hub, **app},
            options=SharedKeyOptions(min_confidence=0.0, sample_overlap=True),
            samplers=samplers,
        ).candidates[0]
        assert cand.references[0].overlap == 1.0
        assert any("value overlap" in e for e in cand.evidence)

    def test_low_overlap_is_inconclusive_and_never_vetoes(self):
        """Truncated per-side samples cannot prove absence, so a low overlap
        must not remove the candidate or subtract confidence."""
        hub, app = self._schemas()
        samplers = {
            "hub": _FakeSampler(
                ratios={("accounts", "account_id"): 1.0},
                values={("accounts", "account_id"): ["x", "y"]},
            ),
            "app": _FakeSampler(values={("events", "account_id"): ["a", "b"]}),
        }
        sampled = infer_shared_keys(
            {**hub, **app},
            options=SharedKeyOptions(min_confidence=0.0, sample_overlap=True),
            samplers=samplers,
        ).candidates[0]
        unique_only = infer_shared_keys(
            {**hub, **app},
            options=SharedKeyOptions(min_confidence=0.0, sample_overlap=True),
            samplers={"hub": _FakeSampler(ratios={("accounts", "account_id"): 1.0}), "app": None},
        ).candidates[0]

        assert sampled.references[0].overlap == 0.0
        assert sampled.confidence == unique_only.confidence
        assert any("inconclusive" in e for e in sampled.evidence)

    def test_a_broken_sampler_degrades_to_name_evidence(self):
        class Boom:
            def distinct_ratio(self, table, column):
                raise RuntimeError("connection reset")

            def sample_values(self, table, column, limit=1000):
                raise RuntimeError("connection reset")

        hub, app = self._schemas()
        cand = infer_shared_keys(
            {**hub, **app},
            options=SharedKeyOptions(min_confidence=0.0, sample_overlap=True),
            samplers={"hub": Boom(), "app": Boom()},
        ).candidates[0]
        assert cand.hub.distinct_ratio is None
        assert cand.confidence > 0


# ── Name helpers ─────────────────────────────────────────────────────


class TestNameHelpers:
    @pytest.mark.parametrize(
        "column,expected",
        [
            ("account_id", "account"),
            ("accountId", "account"),
            ("accountid", "account"),
            ("tenant_uuid", "tenant"),
            ("account", None),
            ("id", None),
            # Value-shaped suffixes must NOT look like entity references, or
            # every address-bearing schema proposes a bogus "Postal" hub.
            ("postal_code", None),
            ("customer_code", None),
            ("invoice_no", None),
        ],
    )
    def test_entity_of(self, column, expected):
        assert entity_of(column) == expected

    def test_value_shaped_columns_never_become_hubs(self):
        a = schema(table("customers", [col("postal_code", "text")]))
        b = schema(table("suppliers", [col("postal_code", "text")]))
        result = infer_shared_keys({"a": a, "b": b}, options=SharedKeyOptions(min_confidence=0.0))
        assert result.candidates == []

    @pytest.mark.parametrize(
        "entity,expected",
        [("account", "Account"), ("accounts", "Account"), ("line_items", "LineItem")],
    )
    def test_concept_name_is_singular_pascal(self, entity, expected):
        assert concept_name(entity) == expected


# ── Persistence + CSI export ─────────────────────────────────────────


class TestPersistenceAndExport:
    def _mapping_with_key(self) -> MappingConfig:
        return MappingConfig(
            source_schema="analytics",
            collections={
                "query_events": CollectionMapping(
                    source_table="query_events", target_collection="QueryEvent"
                )
            },
            shared_keys=[
                SharedKey(
                    key="account_id",
                    concept="Account",
                    hub_kind="entity",
                    hub_source="crm",
                    hub_table="accounts",
                    hub_column="account_id",
                    bindings=[
                        SharedKeyBinding(
                            source="clickhouse_analytics",
                            table="query_events",
                            column="account_id",
                        )
                    ],
                    confidence=0.8,
                    method="hub_named_table",
                )
            ],
        )

    def test_shared_keys_round_trip(self):
        config = self._mapping_with_key()
        reloaded = MappingConfig.model_validate_json(config.model_dump_json())
        assert reloaded.shared_keys[0].hub_table == "accounts"
        assert reloaded.shared_keys[0].bindings[0].source == "clickhouse_analytics"

    def test_empty_shared_keys_are_omitted_from_disk(self):
        """Guards the byte-stability corpus: pre-P6.7 mappings must not drift."""
        assert "shared_keys" not in MappingConfig().model_dump()
        assert "shared_keys" in self._mapping_with_key().model_dump()

    def test_csi_emits_join_keys_and_stays_valid(self):
        doc = mapping_to_csi(self._mapping_with_key(), source_type="clickhouse")
        validate_csi(doc)
        join_keys = doc["conceptualModel"]["joinKeys"]
        assert len(join_keys) == 1
        jk = join_keys[0]
        assert jk["key"] == "account_id"
        assert jk["hub"] == {
            "kind": "entity",
            "concept": "Account",
            "source": "crm",
            "table": "accounts",
            "column": "account_id",
        }
        # The binding resolves to this project's conceptual entity.
        assert jk["bindings"][0]["entity"] == "QueryEvent"

    def test_csi_without_join_keys_is_unchanged(self):
        config = MappingConfig(
            collections={
                "t": CollectionMapping(source_table="t", target_collection="T")
            }
        )
        doc = mapping_to_csi(config)
        validate_csi(doc)
        assert "joinKeys" not in doc["conceptualModel"]

    def test_join_key_binding_field_is_the_stored_attribute(self):
        """``binding.field`` must survive a renaming mapping.

        ``field`` means "the stored ArangoDB attribute" everywhere else in a CSI
        document. Emitting the source column here would hand the federated
        executor an attribute that does not exist on the documents it
        bind-joins — and it only diverges when field_mappings renames the
        column, so a non-renaming catalog cannot detect it.
        """
        config = MappingConfig(
            collections={
                "query_events": CollectionMapping(
                    source_table="query_events",
                    target_collection="QueryEvent",
                    field_mappings={"account_id": "accountId"},
                )
            },
            shared_keys=[
                SharedKey(
                    key="account_id",
                    concept="Account",
                    hub_kind="entity",
                    hub_source="crm",
                    hub_table="accounts",
                    hub_column="account_id",
                    bindings=[
                        SharedKeyBinding(
                            source="ch", table="query_events", column="account_id"
                        )
                    ],
                )
            ],
        )
        schema = Schema(
            tables={
                "query_events": Table(
                    name="query_events",
                    columns=[
                        Column(name="event_id", data_type="uint64", is_primary_key=True),
                        Column(name="account_id", data_type="string"),
                    ],
                    primary_key=["event_id"],
                )
            }
        )
        doc = mapping_to_csi(config, schema)
        binding = doc["conceptualModel"]["joinKeys"][0]["bindings"][0]
        stored = {
            spec["field"]
            for spec in doc["arangoPhysicalMapping"]["entities"]["QueryEvent"][
                "properties"
            ].values()
        }
        assert binding["field"] == "accountId"
        assert binding["field"] in stored
        # The SQL column is still carried, for the relational leg.
        assert binding["column"] == "account_id"
