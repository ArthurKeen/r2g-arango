# Note — the denormalization engine's location blocks end-to-end testing

**From:** `relational-schema-analyzer` (RSA) maintainer · **Date:** 2026-08-25
**Subject:** Phase 11's engine lives here; its measurement layer lives in RSA. Neither
side can be tested end to end.
**Companion:** `relational-schema-analyzer/docs/DESIGN-ADDENDUM-denormalization.md` (the
fuller cross-repo analysis, incl. `arango-schema-analyzer`)

---

## The short version

Denormalization detection is split across two repositories, and **the two halves have
never been connected in any test**:

| Half | Where it lives | How it is tested |
| --- | --- | --- |
| **Measure** — `group_single_valued`, `distinct_ratio`, `delimiter_rate` | **RSA** (`fk_inference.py`, in all five samplers) | now real, but only for CSV / DuckDB / Postgres |
| **Decide** — `DenormFinding`, the five detectors, `remediation_hint` | **r2g** (`src/r2g/denorm.py`, 611 lines) | 25 unit tests, all with a **fake sampler** |

r2g's engine is tested against a fake measurer. RSA's measurers are tested (until last
week, barely) without an engine. So the seam between them — the part most likely to
break — is the one thing no test exercises.

**This is not hypothetical.** Asked whether the probes had ever been integration tested,
we ran them against real rows for the first time. Two of the three raised `TypeError`
immediately (`CsvValueSampler.distinct_ratio` and `.delimiter_rate` filtered a Polars
Series with an expression). They had exactly one test between them: a Databricks mock
asserting the methods exist and return the fake `0.5` its own fake cursor was primed
with. A test that proves a method is callable is not coverage.

Fixed in RSA `8533de7`; real-data coverage added in `46dd7b7` (DuckDB, always-on) and
`0cc867c` (live Postgres). **The Postgres probe SQL is correct** — it passed all nine
checks first time. MySQL, SQL Server and Databricks probe SQL still has zero execution
coverage.

## What r2g should know regardless of what it decides

1. **RSA's probes were broken until last week.** If any r2g code path calls
   `distinct_ratio` or `delimiter_rate` through a CSV source, it was raising. Requires
   RSA ≥ 0.7.2 (unreleased at time of writing; on `main`).
2. **`create_value_sampler("duckdb", …)` returned `None`** until `46dd7b7`, so
   value-based analysis on a DuckDB source silently degraded to name-only. There is now
   a `DuckDbValueSampler`.
3. **Two FK-inference target-selection bugs are fixed** in RSA 0.7.1 and on `main`:
   inference now targets any single-column *candidate key* (0.7.1) and any composite
   candidate key (`4ca7abc`), not only the primary key. Previously a schema with a
   surrogate PK beside the natural business key inferred **nothing**. Correcting an
   earlier draft of this note: r2g does **not** inherit these automatically — the pin is
   `relational-schema-analyzer>=0.4.0,<0.5.0`, so the shim currently re-exports 0.4.0.

## The pin is the blocker, and bumping to the latest release is not enough

Verified against the tags rather than recalled:

| RSA version | CSV probes | candidate-key FK targets |
| --- | --- | --- |
| **0.4.0** (r2g's pin) | broken | no |
| 0.7.1 (latest on PyPI) | **still broken** | single-column only |
| `main` / 0.7.2 (unreleased) | fixed | single + composite |

So the sequencing instinct is right — a real-sampler test against broken probes passes
for the wrong reason, because `_safe_probe` swallows the `TypeError` into `None`, the
sampling detectors silently emit nothing, and only the structural detectors are actually
asserted. But the conclusion is that **0.7.2 has to ship first**; there is no published
version with working CSV probes. That is on the RSA side.

Also worth sizing honestly: the bump is 0.4.0 → 0.7.x, three minor versions, not two
behaviour changes. It brings the R2RML export, `ForeignKey.enforced` (omitted from
serialization when true, so fingerprints are unaffected), `DatabricksValueSampler`,
discriminator + taxonomy discovery, `physicalMapping` schema qualification and join-table
parent columns, the declared-key overlay, `samplers.py`, and both FK target-selection
changes. The FK changes are the ones that can move existing suggestions.

## The ask

RSA's addendum recommends **moving `denorm.py` into RSA** (recommendation R1). Recording
the argument and the counter-argument so r2g can weigh it rather than have it asserted:

**For:**
- The engine is already paradigm-neutral: its only inputs are `Schema`/`Table`, the type
  map, and an injected `DenormSampler` Protocol. Nothing about it is Arango-specific.
- The probes it calls **already live in RSA**, so this moves a module *toward* its
  dependency rather than away.
- r2g has already proven the migration pattern — `src/r2g/fk_inference.py` is a 260-line
  re-export shim over RSA's engine. `denorm.py` would follow it, and r2g would keep the
  remediation scaffolding (`MappingConfig` edits), which is correctly r2g's.
- It is the only way an end-to-end test becomes writable, because no single repo
  currently contains the whole feature.
- `arango-schema-analyzer` needs the same detectors for embedded sub-documents and would
  otherwise port them a third time.

**Against:**
- r2g's Phase 11 is shipped and working. Moving it risks a production consumer for the
  benefit of a second consumer that has not asked yet.
- RSA's taxonomy addendum §4 records that `arango-ontoextract` consumes RSA's connectors
  while **bypassing its conceptual layer entirely** — so "extract it and they will come"
  has a poor track record in this stack. Worth confirming ASA will actually consume it
  before moving anything.

**If the answer is no**, that is a legitimate call — but then the end-to-end gap is
permanent by construction, and it would be worth saying so explicitly in
`PLAN-denormalization-analysis.md` §5, whose own §5.2 specifies live integration tests
("seed a table with an embedded lookup (`zip → city,state`) … against the real PG/MySQL
→ assert the expected findings") that appear never to have been written. RSA now has
exactly that fixture and harness, if r2g wants to point at it.

## One thing worth fixing either way

RSA's `samplers.py` is a **second, separate spend path** that `denorm.py` does not use:
`executor_from_connection` adapts a DB-API connection and `make_value_enumerator` /
`make_specialization_counter` issue their own SQL through it, bypassing the per-connector
samplers entirely. Anything that governs sampler cost or access (a classification gate, a
byte budget) has to cover both paths or it only holds on whichever one a caller happens
to use. Relevant to r2g's Phase 9 classification gate.

---

# r2g's response — 2026-09-03

**Decision: R1 declined.** `denorm.py` stays in r2g. The end-to-end gap is closed
here instead, by testing the engine against a **real** sampler rather than moving
the engine to where the samplers live.

## What was done

| | |
| --- | --- |
| **Pin** | `relational-schema-analyzer` `>=0.4.0,<0.5.0` → `>=0.7.2,<0.8.0` |
| **§5.2 tests** | `tests/integration/test_denorm_real_sampler.py` — real `CsvValueSampler`, real `zip → city,state` fixture, always-on (no DB) |
| **FK guard** | `tests/test_fk_inference.py::TestCandidateKeyTargeting` |
| **Result** | 1687 passed; three-version jump, zero breakage |

**The ceiling was the real blocker, and it was worse than "behind".** r2g pinned
`<0.5.0`, so it could not install *any* of the fixes it was described as
inheriting "automatically" through the re-export shim. It inherited nothing. And
because both 0.4.0 and 0.7.1 carry the broken probes, only 0.7.2 makes a
real-sampler test pass for the right reason — a bump to merely "latest" would
have produced a green suite proving nothing.

**Silence was not taken as compatibility.** The FK-inference change moved no test
expectation, which could equally mean *compatible* or *not covered*. Checked
directly: a surrogate PK beside a natural business key now infers
`orders.account_id → accounts.account_id` where it previously inferred nothing.
Pinned by test, because the pin admits any 0.7.x — and because P6.7 shared-key
inference is built on exactly that shape, so this improves its inputs too.

**The new tests were mutation-checked.** A test written against a *fixed*
dependency proves nothing about whether it would catch the *bug*. Injecting
`TypeError` into `group_single_valued` makes the embedded-lookup test fail, with
`denorm_probe_failed` logged — confirming both that the coverage is real and that
the failure mode is silent narrowing, not a crash.

## Where the R1 argument was narrower than it looked

The real-`CsvValueSampler` route defeats "no repo can test this" **for CSV only**.
Postgres and MySQL denorm coverage still needs a live database, and SQL Server and
Databricks probe SQL remains unverified. For those paths the original argument
stands: no single repo contains the whole feature. Declining R1 buys a testable
CSV seam, not a testable feature.

## On the second spend path — not reachable from r2g

`samplers.py`'s `executor_from_connection` / `make_value_enumerator` /
`make_specialization_counter` are **not reachable from r2g**, directly or
transitively. r2g imports only `fk_inference` (10 sites), `connectors` (7) and
`types` (4); nothing inside RSA calls those functions either — they are re-exported
in `__init__.py` for external callers. So for r2g this is latent, not active.

**But looking for it surfaced a live one.** r2g's Phase-9 classification gate
covers the LLM path and *not* the two value-sampling paths:

| Path | Emits | Gated? |
| --- | --- | --- |
| `llm/sampling.py` | values, off-machine | **Yes** — `exceeds_threshold` at `:53` precedes `sample_values` at `:57` |
| `denorm.py --sample` | statistics only | **Hook only** — `no_sample_columns` is populated solely from a CLI flag / API field, never from classifications |
| `fk_inference --sample` | statistics only | **No gate at all** |

`denorm.py:77-79` states that "a column that must not be value-sampled (e.g. a
Phase-9 Restricted / PII column) is never passed to the sampler". That is true only
if the operator types the column names by hand: nothing derives
`no_sample_columns` from the classification map. The docstring describes an
automatic gate; the code implements a manual escape hatch.

Severity is moderate, not severe — these paths emit ratios and overlap counts, not
values, so nothing leaves the process. But Restricted/PII columns *are* read when
`--sample` is used, contrary to what the docstring implies, and a cardinality
statistic over a PII column is itself weakly disclosive. Tracked as drift alert
`r2g_PHASE9-SAMPLING-GATE`.

## Corrections to the note

- The engine's field is `recommended_action`, not `remediation_hint`.
- Everything else in the note verified exactly: `denorm.py` is 611 lines; its 25
  tests contain **zero** real-sampler references; and `tests/integration/` contained
  no denorm test at all before this change.
