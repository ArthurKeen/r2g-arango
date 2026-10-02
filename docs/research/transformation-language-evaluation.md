# One transformation language, many execution targets — evaluation

**Date:** 2026-09-29
**Brief:** [`transformation-language-research-brief.md`](transformation-language-research-brief.md)
**Probes:** [`transformation-language-probes/`](transformation-language-probes/) — every measured
result below can be re-run from there.

---

## 1. Summary in plain English

**The question.** Today a transformation — "join first and last name", "turn this code into a
label" — has to be written again for each place it runs: Python for bulk loads, AQL on the
server, Kafka SQL for streaming, the source database's own SQL for pushdown. Could we write it
once and have r2g translate it for each target? And is Google's CEL, or spreadsheet-style
formulas, the right language to write it in?

**The short answer.** Writing it once is the right goal, and r2g is already built that way. But
**the hard part is not the language, it is making the answer come out the same everywhere** —
and no product we looked at fully solves that, including r2g today. Changing the syntax will not
fix it. So the recommendation is:

1. **Keep r2g's existing expression language for now**, but define its behaviour precisely —
   what happens with missing values, how numbers become text, how text is cut — and make every
   target follow that definition.
2. **Build the test that catches differences before building any translator.** We built a first
   version of it for this report; it found real disagreements on the first run.
3. **When a target cannot reproduce an expression faithfully, refuse it with a clear message**
   rather than quietly running it somewhere else. The products that quietly fall back are the
   ones with documented wrong answers.
4. **Retarget streaming from ksqlDB to Flink SQL.** Confluent, which makes ksqlDB, now
   recommends Flink for new work.
5. **Revisit CEL later, as the language people type in**, once the test exists. It is well
   specified and Google shipped an official Python version in March 2026. But it translates to
   SQL only for filters, it cannot see other rows or tables, and its strictness about missing
   values would make curators write extra checks.
6. **Do not adopt spreadsheet formulas as the language.** The best-known spreadsheet-formula
   product, Microsoft Power Fx, cannot send `If`, `*`, `/`, `Upper`, `Mid`, `Len` or
   `Concatenate` to any database. When it cannot, it quietly works on the first 500 rows only.
   Microsoft documents that this gives incorrect results.

**The most important finding is about r2g itself.** r2g evaluates an expression one of two ways:
in Python if every part of it is in the evaluator's subset, or by sending it to ArangoDB if any
part is not. Given the same row, **the two disagree on 9 of the 11 subset expressions we tried**
— on missing values, on text-to-number failures, on number formatting, and on what `+` means.
Two consequences:

- **Adding one unsupported function changes the answer for the rest of the expression.**
  `CONCAT(@t, "_", @id)` gives `7_42.0` locally. Wrap it in `SUBSTITUTE(…)`, which the subset
  lacks, and the whole expression goes to the server, where the same `CONCAT` gives `7_42`.
- **Bulk loads never send anything to the server**, so a field whose expression needs ArangoDB
  is silently missing from every bulk-loaded document. It is filled only when the same row
  arrives by stream.

Section 5 has the details; both are worth fixing regardless of anything else in this report.

**What changed the picture during the research:**

- A mature, widely used SQL translator (SQLGlot) **changed the meaning of four of seven simple
  expressions** when translating from Postgres to DuckDB or ClickHouse, and gave no warning.
- Every serious product that aims to "write once, run anywhere" ends up in the same place: one
  language for people to write in, partial pushdown to the database, and a fallback for the rest.
  None of them makes the fallback give identical answers. A well-known Power Query author calls
  the trade-off "purity vs. performance" and says the product leans towards performance.
- Streaming deduplication and lookups are not the same kind of thing as per-field formulas. In
  every system studied they are expressed as query structure, not as formulas. They should stay
  out of the field-expression language.

---

## 2. What we set out to cover, and what we did not

The brief asked for about 25 products. This report covers **15 with sourced findings**. It also
runs **empirical probes on 7 engines**: PostgreSQL, DuckDB, ClickHouse, ArangoDB, r2g's
evaluator, Python and CEL.

The brief asked for all ten reference transformations to be written in every shortlisted
candidate and compiled for three targets. **We did not do that literally.** Instead:

- For the seven scalar items we ran nine probes on seven engines (P1–P7, with item 3 split into
  P3a and P3b), choosing the edge cases where engines are known to differ. We added P8, integer
  division, which is not one of the reference items but is a common source of divergence. This
  produced more evidence than a clean-path matrix would have.
- For items 8–10 we tested *whether they can be expressed at all*, because that is what separates
  the candidates.

**Not covered:**

- Systems the brief named that this pass did not research, so no finding about them should be
  read into their absence:
  - Graph loaders: Neo4j, Amazon Neptune, Ontotext GraphDB.
  - Unified batch and streaming: Spark Structured Streaming, Materialize, RisingWave.
  - ETL and semantic layers: Informatica, Talend, SSIS, Matillion, LookML, Cube, AtScale, and
    the dbt Semantic Layer / MetricFlow. dbt's cross-database macros are covered; its semantic
    layer is not.
  - CEL adopters: Firebase. Kubernetes, Envoy and GCP IAM are covered.
- PuppyGraph's support for computed properties. Its schema documentation returned 404, so this is
  unverified.
- Usability studies on whether spreadsheet syntax reduces authoring errors. **We found none**, so
  that claim remains untested rather than refuted.

The report says "not verified" wherever that applies.

---

## 3. Part 1 — what comparable systems do

### 3.1 Landscape

| System | What people write | What it becomes | When pushdown isn't possible | Documented divergence |
|---|---|---|---|---|
| **Microsoft Power Query (M)** | M formula language | Source SQL via "query folding" | Evaluates locally; every later step stops folding too | Yes — `=` is case-sensitive in M, but folded to SQL Server it follows the column's (often case-insensitive) collation, so `"Joe"` also matches `"joe"` [^pq1][^pq2] |
| **Microsoft Power Fx** (Power Apps) | Excel-style formulas | OData / SQL / Dataverse queries ("delegation") | Evaluates locally on **the first 500 rows** (max 2,000) | Yes — Microsoft: "the query might return incorrect results"; `Average` over a million rows averages 500 [^pfx] |
| **Trifacta / Google Cloud Dataprep** (now Alteryx Designer Cloud) | Wrangle recipe language | BigQuery SQL, Spark, Dataflow | Translates "fully or partially"; the rest runs on Dataflow | Not found [^tri1][^tri2] |
| **Sigma Computing** | Spreadsheet formulas | Warehouse-dialect SQL | Feature availability varies by warehouse; pass-through functions take the warehouse's own semantics | By design — formulas run with the warehouse's semantics [^sig1][^sig2] |
| **Stardog virtual graphs** | SMS2 (SPARQL-based mapping) | SQL against the source | **Restricts the language**: SQL-backed mappings allow only `template` and type casts in `BIND`; CSV/JSON allow all SPARQL functions | Avoided by restriction [^sd] |
| **Ontop** (r2g's federated relational leg) | R2RML mappings | SQL rewriting of SPARQL | Transformations live as SQL inside the mapping (`rr:sqlQuery`) | Inherits source semantics [^ontop][^r2rml] |
| **RML / YARRRML / Morph-KGC** | YAML mappings plus FnO functions | Materialised RDF | Functions run in the engine (a GREL subset, or Python UDFs) | n/a — no pushdown [^rml1][^rml2] |
| **TigerGraph loading jobs** (the model for r2g's mapping UI) | GSQL token functions | Evaluated during the load | User-defined functions are written in C++ (`tokenbank.cpp`) | n/a — load-time only [^tg] |
| **dbt** | SQL plus Jinja macros | Warehouse SQL | Cross-database macros (`dbt.concat`, …) select a per-adapter implementation via `adapter.dispatch` | Delegated to each macro author [^dbt] |
| **Malloy** | Malloy semantic language | SQL for 8 dialects (DuckDB, BigQuery, Postgres, MySQL, Snowflake, Trino, Presto, Databricks) | n/a — SQL targets only | Not found [^mal] |
| **Apache Beam SQL** | Calcite SQL (ZetaSQL removed in 2.68) | Beam PTransforms for batch *and* streaming | n/a — one engine model | Not found [^beam] |
| **Apache Flink SQL** | SQL with window table functions | Batch and streaming jobs | n/a | Not found; dedup is a `ROW_NUMBER()` query shape, not a function [^flink] |
| **ksqlDB** (r2g's P5c.1.7 target) | ksqlDB SQL | Kafka Streams | n/a | — Confluent: *"For new stream processing workloads, Confluent Platform for Apache Flink® is the recommended stream processing engine."* [^ksql] |
| **Substrait** | (none — it is an intermediate plan format) | DuckDB (community extension), DataFusion, Velox | n/a | Integration still maturing: DataFusion ignored an aggregate phase in a Substrait plan (issue #24967) [^sub1][^sub2] |
| **CEL adopters** (Kubernetes, Envoy, GCP IAM) | CEL | Embedded interpreter | n/a — evaluated, not translated | — [^celblog] |

### 3.2 What the market has converged on, and where it has not

**Converged:**

1. **One language to write in; partial pushdown; a local fallback.** Power Query, Power Fx and
   Trifacta all work this way. So does r2g's current design (Python evaluator plus AQL
   delegation). It is the dominant pattern.
2. **Two layers, never one.** Per-row expressions are one layer. Joins, windows,
   deduplication and aggregation are a different layer — query structure, or steps in a pipeline.
   Flink deduplicates with a `ROW_NUMBER()` query shape; Power Query does it with steps. No
   product has a formula language that also handles windowed state.
3. **Virtual graphs restrict the language to what they can push down.** Stardog is explicit about
   this. Ontop and R2RML avoid the question by making the transformation *be* source SQL. The
   consequence for r2g: **federated query and virtual graph both reduce to "emit correct SQL for
   each source database."** They are not separate problems from pushdown.

**Not converged — the open problem:**

4. **Identical results across routes.** Nobody delivers this. The two mature Microsoft products
   document that the same formula can return different results folded versus local (Power Query)
   and complete versus truncated results (Power Fx). A Power Query author describes the trade-off
   as "purity vs. performance" and says the product leans "somewhat towards performance."[^pq2]
   **The products that fall back silently are the ones with documented wrong answers. The one
   that refuses — Stardog — avoids them.**

---

## 4. Part 2 — measured behaviour

### 4.1 Experiment 1: does the same intent give the same answer?

Each probe was written the natural way for each engine and run live. `err` means the engine
raised an error; for CEL it means an error value came back (see 4.4).

| Probe | Postgres | DuckDB | ClickHouse | ArangoDB (AQL) | r2g evaluator | Python | CEL |
|---|---|---|---|---|---|---|---|
| **P1** `UPPER(first)` + `' '` + last, first missing | `\|\|`: NULL · `CONCAT`: `' Smith'` | `\|\|`: NULL · `CONCAT`: `' Smith'` | NULL | `' Smith'` | `' Smith'` | err | err |
| **P2** first non-missing of `''`, missing, `'unknown'` | `''` | `''` | `''` | `NOT_NULL`: `''` · `\|\|`: **`'unknown'`** · `??`: **syntax error** | `''` | `''` | `''` |
| **P3a** `'00123'` → number | 123 | 123 | 123 | 123 | 123 | 123 | 123 |
| **P3b** `'12a'` → number | err | err (`TRY_CAST`: NULL) | err (`OrNull`: NULL) | **0** | **NULL** | err | err |
| **P4** band for a missing score (`<30` low, `<70` medium, else high) | high | high | high | **low** | high | err | err |
| **P5** key from 7 and 42.0 | `'7_42'` | **`'7_42.0'`** | `'7_42'` | `'7_42'` | **`'7_42.0'`** | `'7_42.0'` | `'7_42'` |
| **P6** `'2026-03-29 01:30+02:00'` as text | `'2026-03-28 23:30:00+00'` (server time zone, UTC here) | **`'2026-03-28 18:30:00-05'`** (host time zone) | `'2026-03-28 23:30:00'` | `'2026-03-28T23:30:00.000Z'` | — | `'…23:30:00+00:00'` | `'…23:30:00Z'` |
| **P7** first 3 characters of `'Zürich'` | `'Zür'` | `'Zür'` | **`'Zü'`** (bytes); `substringUTF8`: `'Zür'` | `'Zür'` (0-based); with SQL-style `(…, 1, 3)`: **`'üri'`** | `'Zür'` | `'Zür'` | `'Zür'` |
| **P8** `7 / 2` | **3** | 3.5 | 3.5 | 3.5 | 3.5 | 3.5 | **3** |

Plain-English reading:

- **Only one of the nine probes (P3a) got the same answer everywhere**, and that was the easy
  case.
- **P1:** in Postgres and DuckDB, `||` and `CONCAT` disagree *within the same engine*.
- **P4:** Postgres, DuckDB and ClickHouse say a missing score is `high`, AQL says `low`, and
  Python and CEL refuse. That's three different outcomes for one line of logic.
- **P5:** the same record gets two different identity keys depending on the engine. A loader
  that computed keys on more than one engine would create duplicate vertices. r2g does not do
  this today: it builds `_key` from the primary key, and expressions cannot write `_key`.
- **P6:** Postgres and DuckDB both render the timestamp in the *session* time zone — Postgres
  uses the server's setting, DuckDB the host's. The probe records both settings.
- **P7:** ClickHouse's `substring` counts bytes, so it splits accented characters.

### 4.2 Experiment 2: write once, let a mature transpiler translate

We wrote each probe once in Postgres SQL, translated it with SQLGlot 30.20 (33 dialects), and ran
the result on each engine.

| Expression (written once, Postgres) | Postgres | DuckDB (translated) | ClickHouse (translated) |
|---|---|---|---|
| `UPPER(NULL) \|\| ' ' \|\| 'Smith'` | NULL | same | same |
| `COALESCE('', NULL, 'unknown')` | `''` | same | same |
| `CAST('12a' AS INTEGER)` | error | error — same outcome | **NULL** — bad data silently accepted |
| `CASE` on a NULL score | high | same | same |
| key from 7 and `42.0::double` | `'7_42'` | **`'7_42.0'`** | same |
| `SUBSTRING('Zürich', 1, 3)` | `'Zür'` | same | **`'Zü'`** |
| `7 / 2` | 3 | **3.5** | **3.5** |

**Four of the seven expressions changed meaning on at least one target.** Every translation was
valid SQL, and none produced a warning. This matches SQLGlot's own position — it is "a
transpiler, not a validator"[^sqlg] — and academic work reporting that SQLGlot can generate
"syntactically valid but semantically incorrect queries."[^crack]

SQLGlot has **no Flink, ksqlDB or AQL dialect**, so it cannot reach r2g's streaming or graph
targets at all.

### 4.3 Experiment 3: can the non-scalar items be expressed at all?

| Item | CEL | SQLGlot | Flink SQL | r2g expression language |
|---|---|---|---|---|
| **8** code → label via a reference table | **No** — compile error: undeclared reference to `status_labels`. Possible only if the host program pre-loads the table | Yes, as a SQL join | Yes, as a join | No |
| **9** latest record per key in a 5-minute window | No | **Cannot parse** Flink's `TUMBLE(...)` window syntax in any dialect | Yes — `ROW_NUMBER()` over a window TVF | No |
| **10** order count per customer as a node property | No — `orders` undeclared | Yes, as `GROUP BY` | Yes | No |

This confirms 3.2's second point from the other direction: **items 8–10 are query structure, not
formulas.** They belong in r2g's mapping model — joins, windows and aggregations as declared
mapping features — not inside the field-expression string.

### 4.4 CEL in practice

- **Official Python support now exists.** Google released `cel-expr-python` on 2026-03-03. It
  wraps the official C++ implementation "to ensure maximum consistency with CEL semantics." It is
  Apache-2.0 licensed, requires Python ≥ 3.11 and ships a native extension (we installed 0.1.3, a
  macOS arm64 wheel). Its repository is **read-only**, with no outside contributions yet. The
  community `cel-python` (Cloud Custodian, 0.5.0) is still maintained.[^celpy][^celcc]
- **CEL-to-SQL exists, but only for filters.** `cel2sql` (Go; ports for .NET and Java) turns CEL
  into SQL **`WHERE` conditions** for Postgres, MySQL, SQLite, DuckDB and BigQuery. It does not
  produce value expressions. Every r2g field transformation *is* a value expression.[^c2s]
- **CEL is strict, and that cuts both ways.** A missing value in a comparison or string
  operation is an error, not NULL (P1 and P4 above). That is good for consistency across targets,
  because it refuses rather than guesses. But every column that can be missing then needs an
  explicit guard, which is a real cost for curators.
- **Errors come back as values, not exceptions.** `eval()` returned a value whose `type()` is
  `ERROR`, and whose `value()` is the message text. A caller that doesn't check the type will
  store `"UNKNOWN: No matching overloads found…"` as the field's data. Any integration must check
  the type.
- **Integer division truncates:** `7 / 2` is `3`, matching Postgres and differing from AQL,
  DuckDB and ClickHouse.

### 4.5 The spreadsheet hypothesis

- **Evidence against pushdown.** Microsoft's own delegation documentation says Power Fx does
  **not** delegate `If`, `*`, `/`, `Mod`, `Text`, `Value`, `Concatenate`, `Lower`, `Upper`,
  `Left`, `Mid` or `Len` — "can't be delegated to any data source" in the case of `Mid` and
  `Len`. That rules out pushdown for reference items 1, 3, 4, 5 and 7. When it cannot delegate,
  it processes only the first 500 rows.[^pfx]
- **Sigma** does compile spreadsheet formulas to warehouse SQL. Formulas run with each
  warehouse's semantics, and features vary by warehouse.[^sig1][^sig2] That is the divergence
  problem, accepted.
- **Evidence for the hypothesis:** we found no study or vendor data showing that
  spreadsheet-style syntax reduces authoring errors. The claim is untested, not disproven.
- **Our assessment:** a familiar-looking surface is attractive for curators. But spreadsheet
  semantics — blank equals empty text, 1-based positions, implicit conversion — add to the very
  differences measured in 4.1. If a friendlier surface is wanted, it is better built as a
  **visual formula builder over r2g's own defined semantics** than by adopting a spreadsheet
  language's semantics.

---

## 5. Findings about r2g itself

These came out of the probes and stand on their own.

### 5.1 The local evaluator and the server disagree

`src/r2g/expressions.py` describes itself as a "safe subset of AQL." When an expression falls
outside the subset, the streaming path sends it to ArangoDB instead (P5c.1.5). We gave the
**same expression and the same row** to both. The server side used r2g's own
`NodeTransformer.build_delegation_query` and passed the row as the `@rows` bind variable, as
`StreamingPipeline._apply_delegation` does (`probe_delegation.py`):

| Expression | Row | r2g local | ArangoDB 3.12.9 |
|---|---|---|---|
| `TO_NUMBER(@x)` | `x = "00123"` | `123` | `123` |
| `TO_NUMBER(@x)` | `x = "12a"` | `null` | **`0`** |
| `@s < 30 ? "low" : (@s < 70 ? "medium" : "high")` | `s = null` | `"high"` | **`"low"`** |
| `CONCAT(@t, "_", @id)` | `t = 7, id = 42.0` | `"7_42.0"` | **`"7_42"`** |
| `UPPER(@first) + " " + @last` | `"Ada"`, `"Lovelace"` | `"ADA Lovelace"` | **`0`** |
| `@n + "1"` | `n = 1` | `"11"` | **`2`** |
| `@x + 1` | `x = null` | `null` | **`1`** |
| `@x * 2` | `x = null` | `null` | **`0`** |
| `@x / 0` | `x = 1` | `null` | `null` |
| `COALESCE(@a, @b)` | `a = null, b = "z"` | `"z"` | **error: unknown function** (`ERR 1540`) |
| `@a ?? @b` | `a = null, b = "z"` | `"z"` | **syntax error** (`ERR 1501`) |

**Nine of the eleven disagree.** Every expression in the table is inside the subset, so r2g
evaluates all of them locally today, on every load path. The divergence reaches a document in
two ways:

1. **An unsupported function moves the whole expression.** Routing is decided per expression:
   if any part is outside the subset, all of it is delegated. `CONCAT(@t, "_", @id)` gives
   `"7_42.0"`; `SUBSTITUTE(CONCAT(@t, "_", @id), " ", "")` is delegated and gives `"7_42"`. A
   curator who adds one function to an expression silently changes how the rest of it behaves.
2. **Bulk loads drop delegated fields.** `NodeTransformer.transform_row` leaves a delegated
   target unset for the server to fill. Only the streaming pipeline calls the server; the bulk
   path (`src/r2g/main.py`) does not. A bulk-loaded document therefore lacks the field
   altogether — the probe shows `{'id': 1, 'name': 'Ada Lovelace', '_key': '1'}` for a mapping
   that defines `slug` — while the same row loaded by stream has it. P5c.1.6 records the bulk
   delegation path as open, but not that the field goes missing without an error.

Record identity is **not** affected: `_key` comes from the primary key, and `validate_config`
rejects expressions that target `_key` or any other reserved attribute.

### 5.2 The subset is not a subset of AQL

The evaluator accepts constructs that AQL does not have, or that mean something else in AQL:

- **`??` and `COALESCE`** are not AQL. ArangoDB 3.12.9 rejects `??` as a syntax error and
  `COALESCE` as an unknown function. AQL's equivalent is `NOT_NULL`.
- **`+` on text.** The evaluator joins strings with `+`. AQL converts both sides to numbers, so
  `UPPER(@first) + " " + @last` — reference item 1, written the way the brief writes it — gives
  `0` on the server.
- **Missing values in arithmetic.** The evaluator's docstring, PRD P5c.1.4 and the brief all
  describe "AQL-style null propagation." AQL does not propagate null in arithmetic. It treats
  null as `0`, so `null + 1` is `1`.

Today these matter only when an expression also contains something outside the subset, because
only then is it sent to the server. But P5c.1.7's plan to translate "the canonical AQL-flavoured
expressions" would inherit every one of them.

### 5.3 Which side is right is a decision, not a bug report

AQL's own answers are debatable: treating a missing value as smaller than every number (so it is
`low`) and as `0` in arithmetic, turning `"12a"` into `0`, and turning text into numbers under `+`. Copying them exactly may not be what r2g wants. The finding
is that **r2g has not decided**, so the two routes give different answers. The fix is to decide,
write the decision down, and make both routes follow it.

### 5.4 The streaming target should be revisited

P5c.1.7 targets ksqlDB. Confluent now recommends Flink for new stream processing, and keeps
ksqlDB "fully supported for existing applications."[^ksql] Building a new translator for ksqlDB
today means building it for the engine its vendor has stopped recommending.

### 5.5 r2g already falls back silently for KSQL and Python

Found during the PRD sync that followed this research. The config model allows `engine: "ksql"`
and `"python"` (`src/r2g/types.py:228`), and the mapping UI offers "KSQL (streaming)" as a
choice. But:

- `validate_config` checks only expressions whose engine is `aql` (`src/r2g/config.py:253`).
- `NodeTransformer` passes any non-AQL expression through **unchanged**, logging only a warning
  (`src/r2g/transformers/node_transformer.py:64`).

A curator who writes a KSQL expression therefore gets the raw source value loaded, with no error.
That is the Power Fx pattern from section 3.2 inside r2g.

**This is specified behaviour, not a code defect.** PRD P5c.1.4 (Done) requires that
"un-compilable / non-AQL expressions fall back to identity pass-through with a structured-log
warning," and the code does exactly that. The recommendation is therefore a **requirement
change**: amend P5c.1.4 so that `validate_config` rejects non-AQL engines until a backend exists.
That amendment goes through PRD review (`/prd-sync`, as a proposed patch). The code should not
change ahead of it.

---

## 6. Decision table

Cells show whether the candidate reaches each delivery path natively, needs an adapter r2g would
build, or can't reach it. A **†** marks paths reached only as SQL whose meaning, per 4.2, is not
preserved.

| Candidate | 1 Bulk (Python) | 2 AQL batch | 3 Streaming | 4 Source SQL | 5 Federated | 6 Virtual graph |
|---|---|---|---|---|---|---|
| **A. r2g expression language + r2g-defined semantics + per-target generators** *(recommended)* | native | native | adapter (Flink) | adapter | adapter (emits source SQL) | adapter (emits source SQL) |
| **B. CEL as the surface + r2g generators** | native (official Python) | adapter | adapter | adapter (`cel2sql` covers filters only) | adapter | adapter |
| **C. SQLGlot as the intermediate form** | adapter | not possible (no AQL) | not possible (no Flink/ksqlDB) | native† (33 dialects) | native† | native† |
| **D. Substrait as the intermediate form** | adapter (DuckDB/DataFusion) | not possible | no evidence found | not possible for Postgres/Snowflake/ClickHouse (consumers are engines like DuckDB and Velox, not these databases) | not possible | not possible |
| **E. Spreadsheet-formula surface (Power Fx-style)** | adapter | adapter | adapter | adapter — flagship product cannot push down most scalar functions | adapter | adapter |
| **F. Flink SQL everywhere** | adapter | not possible | native | via translation† | via translation† | via translation† |

---

## 7. Recommendation

**Pairing:** keep **r2g's expression language as the surface**, with **r2g's own parsed
expression tree as the intermediate form**. Put **r2g-defined semantics and a differential
conformance suite** at the centre, and write **per-target generators** that must pass it. Use
SQLGlot only as a *rendering* helper for SQL dialects (quoting, type names), never as the thing
that decides meaning.

**Why this and not the others:**

- **Runner-up 1 — CEL as the surface (B).** It has the best-specified language and a real type
  checker, and now official Python support. It lost on present evidence because:
  - its SQL translation covers filters only, not value expressions;
  - its strict missing-value rules would push extra guards onto curators;
  - the official Python version is at 0.1.3 with a read-only repository.

  It remains the strongest option for a later *surface* change, and it would sit on top of the
  same semantics layer and conformance suite. Nothing in the recommendation closes that door.
- **Runner-up 2 — SQLGlot as the intermediate form (C).** It is the most practical way to reach
  many SQL dialects. It lost because it cannot reach AQL or streaming, and because it changed
  meaning in four of seven cases without warning. It still earns a place as a rendering helper
  under A.
- **Spreadsheet formulas (E)** lost on documented evidence (4.5). **Substrait (D)** targets query
  engines rather than the databases r2g reads from. **Flink everywhere (F)** fixes streaming only.

**What this recommendation cannot do, and what r2g gives up:**

- **It does not make every expression run everywhere.** Some expressions will be *refused* on
  some targets — for example a character-accurate substring on a target that cannot provide one
  — rather than quietly run elsewhere. Curators will occasionally see "this expression can't run
  in streaming." That is the intended behaviour, and it is Stardog's approach rather than Power
  Fx's.
- **It keeps joins, windows and aggregations out of the expression language.** Items 8–10 need
  mapping-model features, which is separate work.
- **It is not a new language**, so it does not deliver a friendlier syntax. If that is wanted, a
  visual formula builder over the same semantics is the lower-risk way to get it.

---

## 8. Staged adoption

| Stage | What ships | What it replaces | How we know it worked |
|---|---|---|---|
| **1. Decide the semantics** | A one-page definition: missing values, text-to-number failure, number-to-text formatting, character (not byte) positions, integer division, time zones | The implicit "AQL-style" claim | Every row in the 4.1 table has one agreed answer |
| **2. Conformance suite** | The probes in `transformation-language-probes/` grown into a CI test: every function × every target, results compared | Nothing — this is new | CI fails when any target disagrees with the definition |
| **3. Fix the two routes** | Local evaluator and AQL delegation brought into line with stage 1; `??` and `COALESCE` either translated or removed; bulk loads either evaluate delegated expressions or refuse them | Today's divergent behaviour (section 5) | Every `probe_delegation.py` case agrees; a bulk-loaded and a streamed copy of the same row produce identical documents |
| **4. Source-SQL generator** | Per-dialect generation (Postgres, Snowflake, ClickHouse, MySQL, SQL Server) with a capability table; unsupported expressions refused at compile time with a clear message | Hand-written native SQL | Pushdown results match the definition in the suite; federated and virtual-graph mappings reuse the same output |
| **5. Streaming generator on Flink SQL** | P5c.1.7 retargeted from ksqlDB to Flink | The unbuilt ksqlDB translator | Stream-loaded and bulk-loaded graphs are identical for the reference data set |
| **6. Optional — surface change** | CEL, or a visual builder, parsing into the same tree | The text syntax, if curators struggle with it | Authoring-error rate, measured before and after |

Stages 1–3 are small, stand alone, and fix a live defect. They are worth doing even if nothing
after them is.

---

## 9. Sources

[^pq1]: Microsoft Learn, *Understanding query evaluation and query folding in Power Query* — https://learn.microsoft.com/en-us/power-query/query-folding-basics
[^pq2]: Ben Gribaudo, *Equals Is Not Always Equivalent: When Query Folding Does Not Produce Identical Results* — https://bengribaudo.com/blog/2021/07/23/5890/equals-is-not-always-equivalent-when-query-folding-does-not-produce-identical-results
[^pfx]: Microsoft Learn, *Understand delegation in a canvas app* — https://learn.microsoft.com/en-us/power-apps/maker/canvas-apps/delegation-overview
[^tri1]: Google Cloud, *Introducing Dataprep BigQuery pushdown* — https://cloud.google.com/blog/products/data-analytics/introducing-dataprep-bigquery-pushdown
[^tri2]: Alteryx, *Fast execution with BigQuery Pushdown for Google Cloud Dataprep* — https://www.alteryx.com/blog/bigquery-pushdown-for-google-cloud-dataprep
[^sig1]: Sigma, *A Deep Dive Into Sigma Formulas* — https://www.sigmacomputing.com/blog/sigma-formulas
[^sig2]: Sigma Documentation, *Supported regions, data platforms, and features* — https://help.sigmacomputing.com/docs/region-warehouse-and-feature-support
[^sd]: Stardog Documentation, *Mapping Data Sources* — https://docs.stardog.com/virtual-graphs/mapping-data-sources
[^ontop]: Ontop — https://github.com/ontop/ontop ; W3C R2RML compliance notes — https://github.com/ontop/ontop/wiki/W3C-R2RML-Compliance
[^r2rml]: W3C, *R2RML: RDB to RDF Mapping Language* (`rr:sqlQuery`) — https://www.w3.org/TR/r2rml/
[^rml1]: RML-FNML specification — https://kg-construct.github.io/rml-fnml/spec/docs/
[^rml2]: Morph-KGC documentation — https://morph-kgc.readthedocs.io/en/latest/documentation/
[^tg]: TigerGraph, *Add a User-defined Token Function* — https://docs.tigergraph.com/gsql-ref/4.2/ddl-and-loading/add-token-function
[^dbt]: dbt Developer Hub, *About cross-database macros* — https://docs.getdbt.com/reference/dbt-jinja-functions/cross-database-macros
[^mal]: Malloy Documentation — https://docs.malloydata.dev/documentation/
[^beam]: Apache Beam, *Beam SQL overview* and *ZetaSQL overview* — https://beam.apache.org/documentation/dsls/sql/overview/ , https://beam.apache.org/documentation/dsls/sql/zetasql/overview/
[^flink]: Apache Flink, *Window Deduplication* — https://nightlies.apache.org/flink/flink-docs-master/docs/dev/table/sql/queries/window-deduplication/
[^ksql]: Confluent Documentation, *ksqlDB for Confluent Platform* (last published 2026-09-22) — https://docs.confluent.io/platform/current/ksqldb/overview.html
[^sub1]: Substrait, *Powered by Substrait* — https://substrait.io/community/powered_by/
[^sub2]: apache/datafusion issue #24967 — https://github.com/apache/datafusion/issues/24967
[^celblog]: Google Open Source Blog, *Common Expressions for Portable Policy and Beyond* (2024-06) — https://opensource.googleblog.com/2024/06/common-expressions-for-portable-policy.html
[^celpy]: Google Open Source Blog, *Announcing CEL-expr-python* (2026-03-03) — https://opensource.googleblog.com/2026/03/announcing-cel-expr-python-the-common-expression-language-in-python-now-open-source.html ; repository — https://github.com/cel-expr/cel-python
[^celcc]: cloud-custodian/cel-python — https://github.com/cloud-custodian/cel-python
[^c2s]: observeinc/cel2sql — https://github.com/observeinc/cel2sql ; SPANDigital/Cel2Sql.NET — https://github.com/SPANDigital/Cel2Sql.NET
[^sqlg]: SQLGlot API documentation — https://sqlglot.com/sqlglot.html
[^crack]: *CrackSQL: A Hybrid SQL Dialect Translation System Powered by Large Language Models* — https://arxiv.org/pdf/2504.00882

Measured results (sections 4.1–4.4 and 5) come from the probes in
[`transformation-language-probes/`](transformation-language-probes/), run on 2026-09-29 and
re-run on 2026-10-02 after review. `probe_delegation.py` (section 5.1) was added then.
