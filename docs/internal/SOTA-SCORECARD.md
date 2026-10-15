---
title: r2g state-of-the-art scorecard
status: baseline
version: 1
date: 2026-10-14
review_cycle: quarterly
next_review: 2027-01
related:
  - docs/PRD.md
  - docs/internal/PLAN-key-identification.md
  - docs/internal/PLAN-federation-forge.md
  - docs/internal/PLAN-code-quality-remediation.md
---

# r2g state-of-the-art scorecard

## Purpose

This scorecard measures how close r2g is to the best available way to turn relational data
into a graph, and it measures this with evidence rather than opinion. It answers three questions:

1. Where does r2g stand on each dimension that matters to someone choosing a
   relational-to-graph tool?
2. What evidence supports that position?
3. What would move each dimension up one level?

The scorecard does not claim that r2g leads anything. The baseline below shows that r2g has
not yet produced the evidence a leadership claim needs. That gap is the main finding.

## Current result: 34.0 / 100 (baseline, 2026-10-14)

| # | Dimension | Weight | Level | Contribution |
|---|---|---:|---:|---:|
| 1 | Load correctness | 15 | 2 | 6.0 |
| 2 | Mapping quality with little human effort | 12 | 1 | 2.4 |
| 3 | Key and relationship discovery | 10 | 2 | 4.0 |
| 4 | Change handling | 10 | 2 | 4.0 |
| 5 | Throughput and scale | 10 | 1 | 2.0 |
| 6 | Source breadth | 8 | 2 | 3.2 |
| 7 | Transformation consistency | 8 | 1 | 1.6 |
| 8 | Governance | 8 | 2 | 3.2 |
| 9 | Standards and interoperability | 8 | 2 | 3.2 |
| 10 | Operability | 6 | 2 | 2.4 |
| 11 | Evaluation rigor | 5 | 2 | 2.0 |
| | **Total** | **100** | | **34.0** |

What the result means:

- **r2g is well built but unmeasured.** Most of its features exist and have tests, but none
  has a recorded result on a fixed workload that someone else could inspect. That is the
  difference between level 2 and level 3, and r2g is at level 2 or below on every dimension.
- **Two dimensions are at level 1 for clear reasons:**
  - Throughput has never been recorded; it is only printed at the end of a run.
  - Transformation consistency has four open drift alerts. The same expression can produce
    different results depending on whether it is evaluated locally or handed to the database.
- **The cheapest gain is one benchmark harness.** A harness that writes a dated, inspectable
  report moves throughput to level 3 at once, and evaluation rigor after two releases. It
  measures load correctness and key discovery on the same Forge workloads too. Load
  correctness counts only after P5c.1.6 is closed, because until then bulk load and CDC can
  silently drop a field. See the prioritized backlog.

## Scoring rule

Each dimension gets one evidence level:

| Level | Meaning |
|---:|---|
| 0 | Absent. |
| 1 | Implemented: the capability exists, and any test only shows that it runs. |
| 2 | Internally evaluated: tests check that results are correct, but there is no recorded measurement on a fixed workload. |
| 3 | Internally benchmarked. This requires all of the following: a quantitative threshold; a versioned workload; disclosed inputs and method; and a dated report that someone else can inspect. |
| 4 | Publicly reproducible: an outside party can rerun the benchmark from public inputs and get the same result. |
| 5 | Independently top-performing: an independent party has compared r2g with the reference set, and r2g leads. |

The rules:

- **r2g may not assess itself above 3.** Levels 4 and 5 require someone outside the project.
- **The score formula is fixed.** Each dimension contributes `weight × level / 5`, and the
  score is the sum of the contributions.
- **Weights are frozen for each quarterly cycle.** A weight can change only at a review, and
  the review must give the reason.
- **An unknown competitor result never counts as a win.** If no one has measured a competitor
  on a dimension, r2g cannot claim to beat it there.
- **A level must cite evidence.** The evidence must be a `file:line`, a test, or a report that
  exists at the commit being scored. A level with nothing to cite drops to the highest level
  that has evidence.
- **An open defect caps a dimension.** If an open defect contradicts a dimension's claim, that
  dimension cannot rise above 2 until the defect is closed. A defect contradicts a claim when
  r2g produces a wrong or silently incomplete result. A combination that r2g refuses with an
  error is a scope limit, not a defect. It does not cap the dimension, but the dimension's
  level-3 report must name it.

## Dimension scores

### 1. Load correctness — weight 15, level 2

**The question:** does the graph contain exactly what the source contained, with nothing
dropped, duplicated or altered?

**Evidence:**
- The Forge roundtrip tests load seeded relational data and check the loaded graph against
  the generated artifacts and the spine joins:
  - `tests/integration/test_forge_roundtrip.py:141`
  - `tests/integration/test_forge_roundtrip_snowflake.py:231`
  - `tests/integration/test_forge_roundtrip_clickhouse.py:209`
  - `tests/integration/test_forge_roundtrip_arango.py`
- Per-engine end-to-end tests exist for each load path:
  - `test_stream_e2e.py`, `test_mysql_e2e.py` and `test_mssql_e2e.py` for the relational sources
  - `test_lpg_e2e.py` for the LPG layout
  - `test_temporal_e2e.py` for temporal mode

**Why it is not level 3:**
- The checks are pass/fail inside pytest.
- No report records counts, mismatches or fidelity per table for a named workload version.

**Capped at 2 by an open defect:**
- **P5c.1.6 (open drift alert).** An expression the local evaluator cannot compile is handed
  to ArangoDB, but only the streaming pipeline does that (`src/r2g/streaming/pipeline.py:404`).
  Bulk load and CDC write the document without the expression's target field, with no error.
  `NodeTransformer.transform_row` leaves the field unset (`src/r2g/transformers/node_transformer.py:213`).

**To reach level 3:** close P5c.1.6, then build a harness that loads each Forge seed and
Customer 360 and writes a dated report. The report should include:
- source and target row counts per table and edge collection
- the number of documents that differ field by field from their source rows, with the target
  set at zero
- the workload's version hash

### 2. Mapping quality with little human effort — weight 12, level 1

**The question:** how close is r2g's proposed mapping to the one an expert would write, and
how many edits does a person need to make?

**Evidence:**
- Heuristic naming: `tests/test_naming.py`.
- Ontology proposal: `tests/test_ontology_proposal.py` and `tests/test_rsa_ontology.py`.
- LLM-assisted derivation (Phase 10):
  - `tests/test_llm_grounding.py`
  - `tests/test_llm_live.py:68`, which checks only that a live LLM round trip yields a *valid*
    mapping, not a *good* one

**Why it is level 1:** nothing compares a proposed mapping with an expert's reference mapping.
The suite proves that a mapping is produced, not that it is good.

**To reach level 2:** an expert-reviewed reference mapping for at least Northwind, Chinook and
pagila, plus a test that scores r2g's unedited proposal against it. The score would cover:
- vertex and edge collections matched
- attributes placed correctly
- edits needed

**To reach level 3:** record that score per release, together with the threshold it must meet.

### 3. Key and relationship discovery — weight 10, level 2

**The question:** when a source declares no keys (common in Snowflake), does r2g find the
right primary and foreign keys?

**Evidence:**
- The propose → review → apply loop runs end to end: `tests/test_cli_suggest_keys.py`.
- Declared uniqueness survives a save and a reload, including keys backed only by a unique
  index: `tests/integration/test_declared_uniqueness_parity.py:128`.
- A6, measured live on 2026-10-02 (`docs/internal/PLAN-key-identification.md`):
  - On Customer 360, r2g found 5 of 5 primary keys and 6 of 6 foreign keys.
  - It made no false proposals and used 13 warehouse queries.

**Why it is not level 3:**
- Customer 360 has 5 tables with fewer than 10 rows each, and the profiler itself marks the
  result as weak evidence.
- The measurement was a one-off and was not recorded as a versioned report.
- r2g's own connectors still read no declared UNIQUE constraints; Plan B closes this.
- Known weakness, inherited from relational-schema-analyzer (RSA): its Postgres, MySQL and
  SQL Server value samplers compare against an arbitrary slice of the referenced table. That
  scores a valid foreign key into a large table near zero. The fix belongs in RSA, and reaches
  r2g through its RSA version band.

**To reach level 3:** precision and recall for primary and foreign keys, recorded per release.
- Run it on a schema of realistic size with its constraints removed. pagila or the Forge
  seeds would work; their declared constraints are the answer key.
- Record the query cost alongside the precision and recall.

### 4. Change handling — weight 10, level 2

**The question:** when the source data or schema changes, does the graph follow correctly
without a full reload, and without losing a person's decisions?

**Evidence:**
- CDC:
  - `tests/test_cdc_handler.py`
  - `test_cdc_conflict.py`
  - `test_cdc_pg_listener.py`
  - `test_cdc_delta_transformer.py`
- Kafka: `test_kafka_consumer.py` and `test_kafka_connector.py`.
- Selective reload: `test_selective_reload.py`.
- Schema diff and mapping diff: `test_schema_diff.py` and `test_mapping_diff.py`.
- Config migration: `test_config_migrate.py`.

**Capped at 2 by open defects:**
- **The `migrate-config` defect
  ([ArthurKeen/r2g-arango#3](https://github.com/ArthurKeen/r2g-arango/issues/3), open).**
  `migrate-config` brings back tables a person deliberately excluded, and it strips curator
  comments. A schema change can therefore undo a reviewed decision without any warning.
- **P5c.1.6 (open drift alert).** CDC builds a document without a delegated expression's
  target field and then replaces the whole stored document
  (`src/r2g/cdc/delta_transformer.py:171`, `src/r2g/cdc/conflict.py:184`). An update can
  therefore erase a field that the initial streaming load filled.

**Scope limit, not a cap:** temporal mode is refused in the LPG layout (P13.12, open drift
alert). `CDCHandler` raises an error naming both ways out (`src/r2g/cdc/handler.py:101`), so
no wrong graph is built. The
change workload's report must say that this combination is not covered.

**To reach level 3:** three steps.
1. Close the `migrate-config` defect.
2. Close P5c.1.6 for CDC.
3. Add a scripted change workload: a Forge seed and a sequence of inserts, updates, deletes and
   schema changes, applied through CDC and through `migrate-config`. Record graph-to-source
   agreement after each step, and the time from source commit to graph update.

### 5. Throughput and scale — weight 10, level 1

**The question:** how fast does r2g load, and how does that change as data grows?

**Evidence:**
- Streaming prints elapsed time and rows per second when it finishes (`README.md:135`).
- `--workers` gives parallel streaming.
- `arangoimport` generation is available for bulk loads.

**Why it is level 1:** no throughput figure has ever been recorded. The README describes a
display, not a measurement.

**To reach level 3:** a scale benchmark covering a Forge seed at 3 or more scale factors and
both load paths (direct streaming and `arangoimport`). Record:
- rows per second
- peak memory
- hardware
- versions

The reference point is the baseline from the competitor set: a hand-written script plus
`arangoimport`. If r2g is slower than that, the report should say so.

### 6. Source breadth — weight 8, level 2

**The question:** how many of the sources customers actually use can r2g read, and how well?

**Evidence:** seven source types are registered at `src/r2g/connectors/base.py:83`:

| Source | Evidence |
|---|---|
| PostgreSQL | `test_stream_e2e.py`, Forge roundtrip |
| MySQL | `test_mysql_e2e.py` |
| SQL Server | `test_mssql_e2e.py` |
| Snowflake | Forge roundtrip, live; fakesnow tests |
| ClickHouse | Forge roundtrip |
| CSV | `test_csv_connector.py`, `test_csv_import_generator.py` (unit only) |
| Kafka | `test_kafka_connector.py`, `test_kafka_consumer.py` (unit only) |

BigQuery is documented as **not** a source (`docs/BIGQUERY-TESTING.md`).

**Why it is not level 3:** there is no matrix recording, for each source and each release,
which capabilities pass on a common workload. The capabilities to cover are introspection,
keys, sampling, streaming and CDC.

**To reach level 3:** produce that capability matrix from the Forge suite as a dated report.
Also close Plan B, under which r2g's own connectors read declared UNIQUE constraints.

### 7. Transformation consistency — weight 8, level 1

**The question:** does an expression give the same result however the data is loaded:
streaming, bulk `arangoimport`, or CDC?

**Evidence:** an evaluator exists (`src/r2g/expressions.py`, `tests/test_expressions.py`),
and expressions it cannot compile are delegated on the streaming path.

**Why it is level 1:** four open drift alerts say the paths can disagree:
- **P5c.1.4:** the evaluator is partial.
- **P5c.1.6:** bulk load and CDC must evaluate or reject a delegated expression. A bulk-loaded
  document must never silently omit the expression's target field.
- **P5c.1.7:** KSQL translation has not started, and a KSQL expression today loads the raw
  source value with only a warning (`src/r2g/transformers/node_transformer.py:64`).
- **P5c.2.5:** the UI still offers KSQL and Python.

The transformation-language research (PR #10) covers the longer-term choice of language.

**To reach level 2:** close P5c.1.6, and add a differential test. The test should run every
expression in the test corpus through each load path and assert that all paths agree or all
reject.

**To reach level 3:** record that differential result for each release on a versioned
expression corpus.

### 8. Governance — weight 8, level 2

**The question:** can a data owner control what sensitive data reaches the graph, and prove
what happened?

**Evidence:**
- Phase 9 (classification propagation and entitlement-aware loading) is complete:
  - Masking: `tests/test_governance.py:82`.
  - The threshold gate, which excludes above-threshold columns by default: `:141`.
  - The gate does not mutate the mapping: `:154`.
  - A manifest that records how each field was handled: `:173`.
- Phase 8 external-catalog integration: `test_openmetadata_provider.py` and the live
  `test_openmetadata_e2e.py`.
- Key overlays require review before they apply: `test_cli_suggest_keys.py:110`.

**Why it is not level 3:** there is no recorded audit on a workload with classified columns.
Such an audit would show that 100% of the columns above the threshold were excluded or masked,
and that the manifest accounts for every one.

**To reach level 3:** seed a Forge workload with classification tags, then report the
leak rate (the target is zero) and manifest completeness for each release.

### 9. Standards and interoperability — weight 8, level 2

**The question:** can other tools use what r2g produces, without custom glue?

**Evidence:**
- r2g emits R2RML (`src/r2g/r2rml.py`) and the CSI conceptual schema (`src/r2g/csi.py`).
- Contextual Data Fabric (CDF) consumes both:
  - its Snowflake and ClickHouse adapters
  - its Forge evaluation
  - CDF pins `r2g-arango==0.6.1`
- CDF found a CSI defect (P12.1, conceptual label uniqueness) and reported it to r2g with
  benchmark evidence. r2g resolved it with `export-csi --label-policy`.
- One part of the output has no consumer yet. r2g emits accepted shared join keys as
  `conceptualModel.joinKeys` (`src/r2g/csi.py:696`), but CDF does not read them (open drift
  alert P6.7-E1-ROUTING). The gap is on CDF's side, so it does not cap this dimension.

**Why it is not level 3:**
- The benchmark evidence is CDF's, not r2g's.
- r2g has no conformance report of its own:
  - It does not check its R2RML against the W3C R2RML test cases.
  - It does not run its R2RML through a second engine, such as Ontop or Morph-KGC, and
    compare the results.

**To reach level 3:** run r2g's generated R2RML for the Forge seeds through Ontop and through
Morph-KGC, and report triple-level agreement with r2g's own load. Separately, report the
pass rate on the W3C R2RML test cases that apply to r2g.

### 10. Operability — weight 6, level 2

**The question:** can someone other than the author install, run, recover and upgrade r2g?

**Evidence:**
- r2g is released on PyPI (0.6.1).
- It has these operator features:
  - `validate-config`
  - `--dry-run`
  - retry with backoff
  - `--drop-collections` for re-runs
- It has the Studio UI, bound to 127.0.0.1, and an MCP server with 23 tools.
- CI enforces 80% line coverage.

**Capped at 2:** the `migrate-config` defect
([ArthurKeen/r2g-arango#3](https://github.com/ArthurKeen/r2g-arango/issues/3)) is also an
operability defect, because an upgrade-time migration silently undoes reviewed exclusions.

**To reach level 3:** two steps.
1. Close the `migrate-config` defect.
2. Run a recorded "fresh operator" trial: a clean machine, the README only, and a Forge seed
   loaded. Report the time and the number of times the operator needed help. Repeat the trial
   for each minor release.

### 11. Evaluation rigor — weight 5, level 2

**The question:** how much can a reader trust r2g's own claims?

**Evidence:**
- The CI coverage gate.
- An integration suite against live engines.
- New tests are mutation-checked: the bug is injected to confirm the test catches it
  (`docs/internal/NOTE-denorm-engine-location.md:150`; Plan A's A4 row in
  `docs/internal/PLAN-key-identification.md:36`).
- Plan A's live runs found two bugs, both fixed in RSA (`PLAN-key-identification.md`,
  "Found along the way").

**Why it is not level 3:** this scorecard is the first record of any measured claim. No run
has produced a versioned, inspectable benchmark report yet.

**To reach level 3:** the benchmark harness in the backlog below, once it has produced reports
for at least two consecutive releases.

## Prioritized backlog

The items are ordered by score gained per unit of work. Items 1 and 2 are a single harness.
Item 1's load-correctness gain depends on item 3, because P5c.1.6 caps dimension 1 at 2.

| # | Work | Dimensions moved | Gain |
|---|---|---|---:|
| 1 | **Benchmark harness.** Load each Forge seed plus Customer 360 and write a dated JSON report and a Markdown report. The reports cover row/edge counts, field mismatches, rows per second, memory, the workload hash and versions. | dim 5: 1 → 3; dim 11: 2 → 3 (after two releases); dim 1: 2 → 3 (once item 3 closes P5c.1.6) | 4.0 + 1.0 = **5.0**, then +3.0 with item 3 |
| 2 | **Key-discovery measurement in the harness.** Strip constraints from pagila and the Forge seeds, then record precision, recall and query cost. | dim 3: 2 → 3 | **2.0** |
| 3 | **Close P5c.1.6 and add the cross-path differential test.** | dim 7: 1 → 2; removes the P5c.1.6 cap on dims 1 and 4 | **1.6** |
| 4 | **Close the `migrate-config` defect** ([ArthurKeen/r2g-arango#3](https://github.com/ArthurKeen/r2g-arango/issues/3)). | Removes the cap on dim 10, and with item 3 the cap on dim 4 | enables 2.0 + 1.2 |
| 5 | **Expert reference mappings for Northwind, Chinook and pagila, with a scoring test.** | dim 2: 1 → 2 | **2.4** |
| 6 | **R2RML cross-engine check (Ontop, Morph-KGC).** | dim 9: 2 → 3 | **1.6** |
| 7 | **Source capability matrix from the Forge suite, and close Plan B.** | dim 6: 2 → 3 | **1.6** |
| 8 | **Classified-column leak audit.** | dim 8: 2 → 3 | **1.6** |

Completing 1–3 and 5–8 takes the baseline from 34.0 to 52.8. Adding item 4, the change
workload (dimension 4 to level 3) and the fresh-operator trial (dimension 10 to level 3)
takes it to 56.0. The ceiling for
self-assessment is 60, when every dimension is at level 3. Going past it needs level 4 on at
least one heavily weighted dimension, which means a public, reproducible benchmark. LDBC SNB
is the candidate.

## Benchmarks

| Workload | Role | Status |
|---|---|---|
| Northwind, Chinook, pagila | Classic relational schemas. They provide the expert reference mappings for dimension 2, and pagila's declared keys are the answer key for dimension 3. | Schemas available; no reference mappings yet |
| Customer 360 (constraint-free) | Key discovery against a reviewed overlay, which is the answer key. | Measured once (A6); too small to be level-3 evidence alone |
| Federation Forge seeded workloads | Correctness, throughput, breadth and change handling on generated, versioned data across engines (`docs/internal/PLAN-federation-forge.md`). | Pass/fail tests exist; no recorded reports |
| LDBC Social Network Benchmark | The candidate public benchmark for level 4 | Not started |

## Competitive reference set

**Every entry below is unverified.** No one has measured r2g against these tools. Under the
scoring rules, none of them can be counted as a loss or a win until someone runs a controlled
comparison on a shared workload.

| Tool | Category | Most comparable dimensions |
|---|---|---|
| Neo4j Data Importer | Graph-vendor import tooling | 2, 5, 10 |
| TigerGraph loading jobs and migration tooling | Graph-vendor import tooling | 5, 6, 4 |
| Memgraph migration module | Graph-vendor import tooling | 6, 5 |
| PuppyGraph | Virtual graph over relational data (no copy) | 4, 6, 9 |
| Stardog virtual graphs | Virtual graph / knowledge graph | 9, 8 |
| Ontop | Virtual knowledge graph (R2RML/OBDA) | 9, 7 |
| Morph-KGC | RML/R2RML materialization | 9, 5 |
| RMLMapper | RML reference implementation | 9 |
| AWS DMS into Amazon Neptune | Managed migration into a graph | 4, 5, 10 |
| Hand-written script plus `arangoimport` | **Baseline**: what a customer does without r2g | 1, 5 |

The baseline matters most. Before r2g claims anything against a vendor, it should show that it
beats or matches the baseline on correctness and throughput, and that the baseline costs more
human effort.

## Review cadence and promotion gates

- **The score is reviewed quarterly.** The next review is 2027-01.
- At each review, the reviewer:
  - re-scores every dimension from evidence present at the reviewed commit
  - updates `date`, `version` and `next_review` in the frontmatter
  - rewrites any sentence that a new fact retires, rather than appending beside it
- **Weights do not change mid-cycle.** A weight change is proposed at the review, with a reason.
- **Promotion to level 3 requires the report itself.** It must be committed or linked from
  this file, and it must name the workload version, the inputs, the method and the threshold.
- **Promotion to level 4 requires an outside rerun.** Someone outside the project must
  reproduce the result from public inputs.
- **Any level above 3 claimed by r2g itself is invalid.**
