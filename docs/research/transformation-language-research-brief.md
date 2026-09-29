# Research brief: one transformation language, many execution targets

**Status:** brief — research not yet run
**Output goes to:** `docs/research/transformation-language-evaluation.md`
**Related:** PRD P5c.1.4 (expression evaluator), P5c.1.5 (AQL delegation),
P5c.1.7 (KSQL translation layer — not started); `src/r2g/expressions.py`

---

## Situation

r2g maps relational sources into ArangoDB property graphs. Per-field
transformations are authored as expression strings, and today the author must
choose the execution engine up front — the config model carries
`engine: "aql" | "ksql" | "python"`. The same logical transformation is
therefore written more than once, in a different dialect per delivery path.

The canonical surface today is a hand-written safe subset of AQL
(`src/r2g/expressions.py`, ~715 lines): literals, `@column` bind references,
arithmetic with AQL-style null propagation, comparisons, boolean logic,
null-coalescing `??`, ternary, and 14 string/number functions. Anything outside
the subset is rejected at compile time so the caller can fall back to
server-side evaluation. A translation layer to streaming SQL is specified but
unbuilt (P5c.1.7).

The requester's starting hypotheses are (a) Google's Common Expression Language
(CEL) and (b) spreadsheet-formula syntax as the authoring surface. Treat both
as hypotheses to test, not conclusions to support.

## Delivery paths that need the same transformation

1. **Bulk load** — applied in-memory in Python before JSONL emission
2. **Server-side batch** — an AQL `FOR doc IN @@batch LET … RETURN doc` rewrite
3. **Streaming (Kafka/CDC)** — ksqlDB-compatible SQL, incl. windowed joins and
   stateful transforms
4. **Source-native SQL pushdown** — Postgres, Snowflake, ClickHouse, MySQL,
   SQL Server
5. **Federated query** — R2RML/Ontop for the relational leg, AQL for the graph leg
6. **Virtual graph** — no materialisation; must survive as a view or mapping

These are not one problem. Paths 1–2 are row-at-a-time scalar expressions.
Path 3 needs time and state. Paths 4–6 need the transformation to survive as a
*mapping*, not a procedure. A language excellent at the first can be
structurally incapable of the last. Say so when it happens.

## Two decisions, assessed separately

**A. Authoring surface** — what a data curator types in the mapping UI.
Candidates: spreadsheet/Excel formula syntax, CEL syntax, the current AQL
subset, a SQL scalar-expression subset, a visual builder that emits no text.

**B. Intermediate representation** — the typed AST that backends compile from.
Candidates: CEL's checked AST, Substrait, SQLGlot's AST, Apache Calcite's
RexNode/RelNode, Ibis expressions, Apache Beam's portable pipeline model, or a
purpose-built IR.

These compose freely: a spreadsheet-style surface can parse to a CEL AST, and
CEL syntax can compile to a Substrait plan. Evaluate each on its own axis, then
recommend a pairing. Do not assume one choice implies the other.

---

## Part 1 — Competitive analysis: what comparable systems actually do

Survey how systems that face the same "author once, run in several places"
problem specify transformations. For each: the language, what it compiles or
pushes down to, how it handles the batch/streaming split, and — most
importantly — **where it refuses or silently degrades**. Marketing claims are
not evidence; cite documentation, source code, or issue trackers.

Start from these groups; add others you find and drop any that turn out to be
irrelevant, saying why.

**Graph loaders and virtual-graph products** (the closest peers)
- TigerGraph loading jobs (GSQL loading language and token functions) — note
  r2g's mapping UI was modelled on TigerGraph GraphStudio
- Neo4j (Cypher `LOAD CSV`, APOC, Neo4j Data Importer)
- Stardog virtual graphs (SMS / SMS2 mapping syntax)
- PuppyGraph (virtual graph over SQL sources — schema mapping only?)
- Ontotext GraphDB (virtual repositories via Ontop, OntoRefine)
- Amazon Neptune bulk loader and any transformation support
- RML / YARRRML ecosystem (YARRRML as the "human-friendly" surface over RML)

**Write-once-push-down precedents** (the strongest analogues to the ambition)
- Microsoft Power Query M and *query folding* — a transformation language that
  is folded into source-native SQL where possible and evaluated locally where
  not. Establish exactly when folding breaks and how the user is told.
- Trifacta / Google Cloud Dataprep (Wrangle language compiled to Spark,
  Dataflow and BigQuery)
- Sigma Computing (spreadsheet formulas compiled to warehouse SQL)
- Microsoft Power Fx (Excel-formula language used across the Power Platform)

**Unified batch and streaming**
- Apache Beam (one pipeline model, multiple runners)
- Apache Flink (SQL/Table API in both batch and streaming mode)
- Spark Structured Streaming (same DataFrame API for batch and stream)
- Materialize, RisingWave (streaming SQL databases)
- ksqlDB (current r2g streaming target)

**ETL / ELT and semantic layers**
- dbt (SQL + Jinja; dbt Semantic Layer / MetricFlow)
- Informatica, Talend, SSIS, Matillion expression languages
- LookML, Cube, Malloy, AtScale — models compiled to many SQL dialects

**CEL adopters** — for evidence about CEL in practice rather than in principle
- Kubernetes (ValidatingAdmissionPolicy, CRD validation), Envoy/Istio,
  Google Cloud IAM Conditions, Firebase

**Required output for Part 1:** a landscape table — system, surface language,
IR (if any), targets, batch/stream handling, known limits — followed by a short
section: *"What the market has converged on, and where it has not."* If every
serious product ends up with two languages (e.g. one for scalar expressions, one
for pipelines), say so; that is itself the finding.

---

## Part 2 — Deep evaluation of the shortlisted candidates

Shortlist 4–6 candidates for decision A and 3–5 for decision B from Part 1.
Every shortlisted candidate gets the same treatment — no candidate is dismissed
in a sentence.

### 2.1 Reference transformation set

Express **all ten** of the following in every shortlisted surface language, and
show the compiled output for each IR on at least three targets. Where a
candidate cannot express one, say so and say why. This set is the backbone of
the comparison; it is what turns a feature list into evidence.

| # | Transformation | What it probes |
|---|---|---|
| 1 | Display name: `UPPER(first) + " " + last`, where either may be null | null propagation in concatenation |
| 2 | First non-empty of `phone`, `mobile`, else `"unknown"` | null vs empty-string distinction |
| 3 | `"00123"` → number; and `"12a"` → number | coercion, and failure behaviour |
| 4 | Risk band from a numeric score (`<30` low, `<70` medium, else high) | conditional / CASE |
| 5 | Composite key: `tenant_id + "_" + id` | string building for identity |
| 6 | Source timestamp with offset → UTC ISO-8601 | date and timezone semantics |
| 7 | First three characters of a postcode | **0- vs 1-based indexing** |
| 8 | Status code → label via a reference table | lookup / join — breaks scalar-only languages |
| 9 | Latest record per key within a 5-minute window | state and time — streaming only |
| 10 | Order count per customer, written as a node property | aggregation into the graph |

Items 1–7 are scalar and should be expressible almost everywhere. Items 8–10
are where scalar expression languages stop and plan languages start — they
separate the candidates.

### 2.2 Per-candidate profile

For each candidate, cover:

- **Architecture** — parser, type checker, IR, backends. What is shipped and
  what the adopter builds.
- **The reference set** — expressions and compiled outputs, per 2.1.
- **Semantics** — its null model, type system, coercion rules, indexing base,
  numeric precision. Contrast with AQL's where r2g's existing expressions would
  change meaning; give the expression and both results.
- **Python story** — who maintains the Python implementation, its
  conformance-suite pass rate if one exists, release history, and whether it
  is production-used.
- **Extensibility** — how a new function or a new target backend is added, and
  how much code that takes.
- **Failure modes** — documented bugs, open issues, and known divergence
  between implementations or targets.
- **Governance** — licence, maintainer, cadence, single-vendor risk, and what
  happens to r2g if it is abandoned.

### 2.3 Specific questions on CEL

CEL is designed for embedded *evaluation* — safe, non-Turing-complete,
statically checkable, guaranteed to terminate. It is not designed as a
transpilation source. Establish concretely:

- Does any production system compile CEL to SQL, or to any target other than
  its own interpreter? Name it, or state that none was found.
- Python support: the reference implementation is Go, with C++ and Java.
  Report who maintains the Python implementation, conformance pass rate, and
  release history.
- CEL has no aggregation, windowing or state. Confirm, and state what that
  means for paths 3, 5 and 6 and reference items 8–10.
- The honest value case: a specified grammar, type checker and conformance
  suite replacing ~715 lines of hand-written parser — with every backend still
  to write. Is that worth it?

### 2.4 The spreadsheet hypothesis

The requester is drawn to spreadsheet syntax because curators already know it.
Test that:

- Which products ship it, what they compile to, and what they refuse to support.
- Is there evidence that familiar syntax reduces authoring errors — studies,
  vendor data, usability research — or does it import spreadsheet semantics
  (implicit coercion, error values as data, 1-based string indexing, no
  null/empty distinction) that make paths 3–6 harder?
- Where does it cap out against the reference set?

---

## Evaluation criteria, in priority order

1. **Semantic fidelity across targets** — decisive. Transpilation fails
   silently, not loudly. Using the reference set, show where the same
   expression yields different results on two targets, and how each candidate
   prevents or detects it. A candidate that cannot *detect* divergence is
   disqualified regardless of coverage.
2. **Target coverage** — per delivery path: native, adapter needed, or not
   possible.
3. **Fit** — adoption cost in a Python codebase; whether existing expressions
   convert mechanically. The expression set is small today, so migration cost
   should not dominate.
4. **Authoring ergonomics** — per 2.4.
5. **Governance** — per 2.2.
6. **Testability** — can one expression be run against two targets and the
   results diffed automatically? That is how drift is caught in CI.

## Deliverable

Saved as `docs/research/transformation-language-evaluation.md`:

1. Executive summary — the recommendation in plain English, one page, no jargon.
2. Part 1 landscape table and convergence findings.
3. Part 2 profiles, including the full reference-set matrix.
4. A decision table: shortlisted candidates as rows, the six delivery paths as
   columns, each cell *native / adapter needed / not possible*.
5. The recommended pairing of surface and IR, with the two strongest
   alternatives and why they lost.
6. What the recommendation cannot do, and what r2g gives up.
7. A staged adoption path: what ships first, what it replaces, and what
   measurable result proves it worked.
8. Sources — every factual claim about a product traceable to documentation,
   code or an issue.

If the answer is "keep the AQL subset and build transpilers outward," say so
plainly. Incumbency is a legitimate result; this brief is not pressure to
replace it.
