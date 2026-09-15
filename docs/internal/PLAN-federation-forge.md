# PLAN — Federation Forge: the reverse generator (walking skeleton)

**Status:** in progress — S1 walking skeleton (Postgres only)
**Commissioned by:** contextual-data-fabric ADR-0006 (accepted 2026-09-08),
which assigns r2g the generator core (its D-4): r2g owns the ontology→schema
mapping machinery in both directions, so the *reverse* direction — generate a
physical schema and synthetic data **from** an ontology — lands here.
**Consumer:** the fabric's M15 orchestration (shape descriptors, partitioning
across engines, expected-catalog/golden computation) stays in
contextual-data-fabric. r2g ships only the per-dialect seam:

```
generate(ontology, dialect, seed) -> {ddl, loader, rows}
```

## Why (one paragraph)

Every correctness claim the fabric makes is validated against one hand-built
corpus. ADR-0006's insight: the estate already owns ontology↔schema mapping in
the forward direction (introspect → Auto-Map → CSI). Run it in reverse and
every generated federation is born with its ground truth attached — the
generating ontology IS the expected aligned ontology. The core correctness
property is the roundtrip through the REAL forward pipeline (never through
forge-internal shortcuts):

```
introspect(generate(O)) ≡ O        (ADR-0006 D-3)
```

with `≡` defined honestly: equality up to CC-12 naming normalization and
declared-constraint availability.

## Decisions (r2g-side)

### F-1 · The ontology input is the CSI v1 conceptual model

ADR-0006's shape descriptor names an `ontology.ttl`, but r2g has never parsed
OWL/TTL and the estate's only *validated* ontology interchange contract is CSI
v1 (`schemas/csi_v1.schema.json`), which `arango-schema-analyzer` already
produces in reverse. The forge therefore takes a **conceptual ontology** shaped
exactly like `conceptualModel` in CSI v1 — entities, properties, relationships
— with one extension the skeleton needs: each property carries a JSON `type`
(`integer | float | boolean | string`), which CSI already permits (the fabric's
`arango-cmf.json` emits it today). TTL→conceptual-model conversion is the
fabric orchestration's job when the descriptor lands (S3); if the team wants
TTL ingestion in r2g instead, that is a one-module follow-up, not a rework.
**Flagged for review in the skeleton PR.**

### F-2 · Naming is the declared inverse of CC-12, asserted through the real normalizer

`csi.owl_entity_name` / `csi.owl_property_name` are lossy and non-injective,
so the generator declares one canonical physical spelling and the roundtrip
asserts through the *normalizer*, never against a hoped-for literal:

- class `UsageMetric` → table `pluralize(convert_identifier(name, "snake"))`
  = `usage_metrics`; property `healthScore` → column `health_score`;
- the test asserts `owl_entity_name(generated_table) == input_class` and
  `owl_property_name(generated_column) == input_property` — the exact
  functions the forward CSI emitter runs.

Skeleton guard: the generator **refuses** an ontology whose class names don't
survive its own roundtrip (`owl_entity_name(pluralize(snake(C))) != C`, e.g.
singularize's naive trailing-`s` rule) — fail loudly at generate time, never
at compare time.

### F-3 · One canonical Postgres type per JSON type

`config.DEFAULT_TYPE_MAP` is many-to-one, so the inverse picks one
roundtrip-stable spelling per JSON type and the comparison runs through
`pg_type_to_json_type` on both sides:

| conceptual `type` | generated DDL | introspects back as | JSON type again |
|---|---|---|---|
| `integer` | `bigint` | `bigint` | `integer` |
| `float` | `double precision` | `double precision` | `float` |
| `boolean` | `boolean` | `boolean` | `boolean` |
| `string` | `text` | `text` | `string` |

Temporal/decimal/uuid categories (RSA's `normalized_type_category`) arrive
with the other dialects (S2); the skeleton's four cover every property in the
fabric's live CSIs.

### F-4 · Keys and relationships: declared constraints first

Every entity gets `id bigint PRIMARY KEY`. Every conceptual relationship
`fromEntity → toEntity` becomes a real `FOREIGN KEY` column
`<singular_snake(toEntity)>_id` on the from-table referencing `<to-table>(id)`
— so Auto-Map re-derives the edge as `<from>_to_<to>` and the CSI names it
back to the input's lowerCamel relationship type. The ADR's
constraint-stripped variant (keys recovered by inference OR reported absent —
never silently wrong) is the S2/S3 denormalizer's job, not the skeleton's.

### F-5 · Data synthesis: once, seeded, spine-safe (ADR-0006 D-2/D-5)

`random.Random(seed)` end to end; a descriptor re-run must be byte-identical.
Rows are synthesized per entity in relationship-topological order
(`topo_sort`), and every FK value is drawn from the already-synthesized parent
`id`s — join-spine agreement by construction. Values are type-driven and
deliberately naive (the skeleton's "naive synthesis"); vocabularies,
cardinality shaping, and statistics arrive with the fabric's descriptor knobs.

### F-6 · Collision-free by construction (skeleton)

`csi._resolve_label_collisions` actively mutates conceptual models (qualify
renames; `roles` drops PK-collision labels), so a colliding ontology cannot
roundtrip verbatim. The skeleton generator refuses input ontologies where two
entities share a property label; the roundtrip integration test emits with the
default policy and asserts `provenance.labelCollisions == []`. Deliberate
collisions are a *denormalizer feature* (the injected-collision report is the
expected artifact) — S3.

## Deliverables (this PR)

| # | Artifact | Where |
|---|---|---|
| 1 | This plan | `docs/internal/PLAN-federation-forge.md` |
| 2 | Generator core: `ForgeOntology` loader/validator, `generate(ontology, dialect="postgres", seed)` → `ForgeArtifacts(ddl, load_sql, rows)` | `src/r2g/forge.py` |
| 3 | CLI: `r2g forge generate --ontology o.json --seed 421 --out-dir …` (thin body, function-local imports, validate-before-try) | `main.py` sub-app |
| 4 | Unit tests: determinism (byte-identical re-run), naming inverse through the real `owl_*` normalizers, type inverse through `pg_type_to_json_type`, FK spine agreement, refusal cases | `tests/test_forge.py`, `tests/test_cli_forge.py` |
| 5 | Live roundtrip: temp PG schema ← DDL+load, `PostgresConnector.get_schema` → `generate_default_config` → `mapping_to_csi` → compare to input ontology (D-3, real pipeline, no forge shortcuts) | `tests/integration/test_forge_roundtrip.py` |

Non-goals here: other dialects (S2), partitioner/denormalizer/goldens (S3),
scale knobs (S4), any change to `MappingConfig`/`Schema` serialization
(byte-stability guard stays untouched), PRD phase-table entry (goes through
`/prd-sync` with the user, not a silent edit).

## The honest-fidelity caveat (from recon, worth keeping visible)

`MappingConfig` sits between schema and CSI and carries derived decisions
(join-table flags, edge names). The generator emits physical schemas and the
re-introspection re-derives those decisions independently via Auto-Map —
*those two derivations agreeing is the actual content of the fidelity claim*.
That is exactly what ADR-0006 wants tested (the estate is in the loop), and it
is why the roundtrip test compares CSI conceptual models, not intermediate
artifacts.
