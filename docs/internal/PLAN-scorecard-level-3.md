# Plan — raise the SOTA scorecard to level 3

**Status:** proposed 2026-10-19; nothing started · **Baseline:** 34.0 / 100
(`docs/internal/SOTA-SCORECARD.md`, version 1, 2026-10-14) · **Target:** about 46–47 at the
2027-01 review, 50 as a stretch, 56.0 the quarter after.

## Why

The scorecard rates r2g at level 2 or below on all 11 dimensions. Its features exist and have
tests that check the answers, but none has a recorded measurement on a fixed workload that
someone else could inspect. That is the step from level 2 to level 3. Two dimensions are at
level 1: throughput has never been recorded, and the load paths can disagree about an
expression's result, in some cases by silently dropping a field.

## Approach

1. **Fix silent wrong results before measuring.** A benchmark taken while bulk load and CDC
   can drop a field would measure the bug. The scorecard's cap rule also holds load
   correctness and change handling at level 2 until P5c.1.6 is closed. This is why Phase 1
   comes before the harness, although the scorecard's backlog ranks the harness first by gain
   per unit of work.
2. **Build one harness, and add a measurement to it for each dimension.** Level 3 asks the
   same four things of every dimension: a threshold, a versioned workload, disclosed inputs
   and method, and a dated report. One mechanism can serve all of them.
3. **Compare against independent answers.** Expected values come from Forge's generated rows,
   the source's declared keys and SQL run against the source. They never come from r2g's own
   transformer output; a harness that did that would grade r2g against itself.
4. **Design for an outside rerun.** The Forge seeds and pagila are public and run on the
   compose stack. A harness that runs from one command on those inputs is the path to level 4
   on load correctness, key discovery and throughput, without waiting for LDBC SNB.

## Phase 1 — stop silent wrong results (about 2 weeks; 34.0 → 35.6)

### 1.1 Bulk export refuses expressions it cannot compute (P5c.1.6, bulk)

- **Context:** `transform-all` and `transform-nodes` write documents without the target field
  of any expression that must be evaluated by ArangoDB. They have no database connection.
- **Action:** refuse when the transformers are built, with an error naming the collection and
  the target field, as P5c.1.6 requires. Server-side evaluation behind an `--endpoint` option
  can follow if anyone needs it.
- **Result:** bulk output never silently omits an expression's target field.

### 1.2 CDC evaluates delegated expressions on the server (P5c.1.6, CDC)

- **Context:** CDC builds the document without the field and then replaces the stored
  document (`src/r2g/cdc/conflict.py:184`), erasing a value that the initial streaming load
  filled.
- **Action:** CDC already holds an `ArangoWriter`. Reuse the streaming pipeline's delegation
  step (`build_delegation_query`, `StreamingPipeline._apply_delegation`).
- **Result:** P5c.1.6 is closed, which lifts its cap on load correctness (dimension 1) and
  change handling (dimension 4).

### 1.3 Refuse expression engines that have no backend (P5c.1.4's code gap, P5c.2.5, the silent half of P5c.1.7)

- **Context:** a `ksql` or `python` expression loads the raw source value with only a warning
  (`src/r2g/transformers/node_transformer.py:64`), and Studio still offers both engines
  (`src/r2g/ui/static/index.html:1219`, `:6224`).
- **Action:** `NodeTransformer` raises instead of warning, which covers every load path in one
  place. `validate_config` reports the same error, and Studio offers AQL only.
- **Result:** P5c.2.5 and P5c.1.4's code gap are closed. P5c.1.7 stays open as not built, but
  no longer fails silently. This is a behaviour change: a CHANGELOG `### Changed` entry and a
  minor version.

### 1.4 A test that the load paths agree

- **Context:** nothing checks that streaming, bulk and CDC give the same result.
- **Action:** an integration test that runs every expression in `tests/test_expressions.py`
  and in `docs/research/transformation-language-probes/` through all three paths, and asserts
  that they produce the same document or all refuse.
- **Result:** dimension 7 moves from level 1 to level 2 (**+1.6**).

## Phase 2 — the benchmark harness (about 4 weeks; → 47.2)

### 2.1 Harness and report format

- **Context:** no recorded measurement exists.
- **Action:** a tracked script, `benchmarks/run.py`, that writes
  `benchmarks/reports/<date>-<version>.json` and a matching `.md`. Each report records the
  workload's identity (r2g version, Forge seed and `rows_per_entity`, hashed), software
  versions, hardware, method, thresholds, and pass or fail for each measurement. It lives in a
  tracked path because CI calls it.
- **Result:** one mechanism that every later measurement plugs into.

### 2.2 Load correctness

- **Context:** the Forge roundtrip tests pass or fail inside pytest and record nothing.
- **Action:** for each Forge dialect and for Customer 360, record source and target counts per
  table and edge collection, and diff every document field by field against its source row.
  Threshold: zero mismatches and exact counts.
- **Result:** dimension 1 reaches level 3 (**+3.0**). Depends on 1.2.

### 2.3 Throughput

- **Context:** rows per second is printed at the end of a stream (`README.md:135`) and never
  recorded.
- **Action:** run a Forge seed at three sizes (for example 1,000, 10,000 and 100,000 rows per
  entity) through streaming with one worker and with several, and through `arangoimport`.
  Record rows per second and peak memory. Run the same workload through a short hand-written
  baseline (export, reshape, `arangoimport`), which is the scorecard's competitor baseline.
- **Result:** dimension 5 reaches level 3 (**+4.0**).

### 2.4 Key discovery

- **Context:** the one measurement, A6, used a 5-table demo with fewer than 10 rows per table.
- **Action:** copy pagila and the Forge seeds with their constraints removed, run
  `source suggest-keys`, and score the result against the original declared keys: precision
  and recall for primary and foreign keys, and the number of warehouse queries.
- **Result:** dimension 3 reaches level 3 (**+2.0**). RSA's value samplers compare against an
  arbitrary slice of the referenced table, which may show up as missed foreign keys into
  pagila's larger tables. That fix belongs in RSA and reaches r2g through its version band.

### 2.5 Sensitive-data leak audit (backlog item 8, moved up)

- **Context:** this costs little once the harness exists.
- **Action:** load a Forge workload with classification tags on chosen columns. Count the
  above-threshold columns that reach the graph unmasked (target 0), and the columns the
  manifest fails to account for (target 0).
- **Result:** dimension 8 reaches level 3 (**+1.6**).

### 2.6 A report at every release, and a CI regression check

- **Context:** dimension 11 needs reports from two consecutive releases.
- **Action:** add the harness run to the release checklist. Add a small-scale run to the CI
  integration job against a committed baseline report. A correctness regression fails the
  build; throughput is advisory, because CI hardware is noisy.
- **Result:** dimension 11 reaches level 3 after the second release (**+1.0**).

Customer 360 lives in Snowflake and needs an account to run. Its results are recorded, but
they are not part of the outside-rerun set and do not run in CI.

## Phase 3 — change handling and operability (about 3 weeks, can overlap Phase 2; → 50.4)

### 3.1 `migrate-config` keeps people's decisions

- **Context:** `migrate-config` re-adds tables a curator excluded and drops the comments that
  record why ([ArthurKeen/r2g-arango#3](https://github.com/ArthurKeen/r2g-arango/issues/3)).
  r2g reads YAML with `pyyaml`, which discards comments on load.
- **Action:** a comment-preserving YAML round trip (`ruamel.yaml` is the usual choice, and a
  new dependency) that keeps exclusions. The issue's acceptance test: regenerate a config that
  carries both, and the diff is empty. The CHANGELOG states the contract.
- **Result:** the caps on dimensions 4 and 10 come off. It also clears a blocker that
  Contextual Data Fabric (CDF) has recorded against r2g.

### 3.2 Change workload

- **Context:** nothing measures whether the graph follows changes in the source.
- **Action:** a scripted sequence of inserts, updates, deletes and schema changes on a Forge
  seed, applied through CDC and through `migrate-config`. After each step, record whether the
  graph agrees with the source, and the time from source commit to graph update. The report
  states that temporal mode in the LPG layout is not covered (P13.12, a scope limit).
- **Result:** dimension 4 reaches level 3 (**+2.0**).

### 3.3 Fresh-operator trial

- **Context:** there is no record of anyone outside the project installing and running r2g
  from the README alone.
- **Action:** someone outside the project, on a clean machine, with only the README, loads a
  Forge seed. Record the elapsed time and the number of times they needed help. Repeat for
  each minor release.
- **Result:** dimension 10 reaches level 3 (**+1.2**).

## Phase 4 — next quarter (→ 56.0)

- **4.1 Expert reference mappings** for Northwind, Chinook and pagila (all under `docker/`),
  and a test that scores r2g's unedited proposal against them: collections and edges matched,
  attributes placed correctly, edits needed. Dimension 2 reaches level 2 (**+2.4**). This
  needs an expert's time.
- **4.2 R2RML cross-engine check.** Morph-KGC and Ontop generate triples from r2g's R2RML for
  the Forge seeds, and those are compared with triples derived from r2g's loaded graph. The
  W3C R2RML test cases exercise processors rather than generators, so few will apply; the
  cross-engine comparison carries the evidence. Dimension 9 reaches level 3 (**+1.6**).
- **4.3 Source capability matrix and Plan B.** Dimension 6 reaches level 3 (**+1.6**). This
  comes last because Plan B (`PLAN-key-identification.md`), which moves introspection into
  RSA and reverses a recorded design decision, is the largest item in the backlog for
  1.6 points.

## Before Phase 1 (about a day)

- **A scorecard check in CI.** A script under `scripts/` fails when:
  - a contribution is not `weight × level / 5`, or the total disagrees with the table
  - a level is above 3
  - a level-3 dimension does not link a report that exists

  It also warns when implementation commits have accumulated since the scorecard's `date`.
  Self-assessment documents go stale because no other gate reads prose.
- **Run `/prd-sync`.** The PRD has changed since the last sync.
- **Dimension 11 evidence.** The `/code-quality-review` skill merged in PR #22 after the
  baseline, so the next review can cite it.

## Score path

| After | Score |
|---|---:|
| Baseline (2026-10-14) | 34.0 |
| Phase 1 | 35.6 |
| Phase 2, first release with a report | 46.2 |
| Phase 2, second release with a report | 47.2 |
| Phase 3 | 50.4 |
| Phase 4 | 56.0 |

60 is the ceiling for a self-assessment. Going past it needs an outside party to rerun a
benchmark (level 4).

## Repository obligations

- **PRD.** The harness and the scorecard check are new code with no requirement yet. Each
  phase's PR adds its PRD rows alongside the code, and the commit subject names the PRD edit.
  Phase 1 closes P5c.1.6, P5c.2.5 and P5c.1.4's code gap through `/prd-sync`, with
  `file:line` evidence.
- **CHANGELOG and release.** Phase 1 refuses input that used to load, so it ships as a minor
  release with a `### Changed` entry.
- **Downstream.** CDF pins r2g exactly and adopts each release deliberately. Before it moves
  to the Phase 1 release, check that its mappings use only AQL expressions. No phase changes
  Forge output, so CDF's committed shapes need no regeneration.

## Open decisions

1. **Thresholds.** Load correctness is zero mismatches. The others are open. Starting points:
   - throughput within 1.5× of the baseline's time
   - primary keys at precision ≥ 0.95 and recall ≥ 0.9; foreign keys at ≥ 0.9 and ≥ 0.8
   - operator trial under 30 minutes, with no help
2. **Bulk expressions.** Refuse only, or also evaluate on the server through `--endpoint`.
3. **People.** Who writes the expert reference mappings, and who runs the operator trial.
4. **Releases.** Dimension 11 needs two releases with reports before the 2027-01 review.
5. **Where the `migrate-config` issue lives.** It is on ArthurKeen/r2g-arango, and the
   scorecard links there. It could move to arango-solutions-org/r2g-arango.
