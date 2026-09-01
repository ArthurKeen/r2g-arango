---
marp: true
title: r2g — Relational-to-Graph for ArangoDB
description: Migrate relational data into an ArangoDB graph — or federate it in place with CSI v1 + R2RML.
paginate: true
theme: default
---

<!-- _paginate: false -->

# r2g

## Relational-to-Graph for ArangoDB

**Migrate** your relational data into a graph — or **federate** it in place.

*Open source · Apache 2.0 · `pip install r2g-arango` · Python 3.10+*

---

## The problem

Foreign keys are relationships **trapped as implicit joins**.

Two hard questions:

1. **Move it** — how do you project a relational schema onto a *good* graph ontology, at scale, correctly, and keep it in sync?
2. **Or don't** — some data should stay where it lives (ClickHouse rollups, a Postgres CRM, an ArangoDB graph). How do you ask **one** question across all of it?

r2g answers both.

---

## 1 · Derive a baseline ontology (deterministic)

- **Table → document collection**, PK → `_key`
- **Foreign key → edge**; **join table → edge**
- **50+ type coercions**, composite keys, multi-schema, partitions, PK-less safety

One function turns an introspected schema into a default mapping — a reproducible baseline everything else builds on.

**Sources:** PostgreSQL · MySQL/MariaDB · SQL Server · Snowflake · **ClickHouse** · CSV · Kafka

---

## 2 · Shape it visually — the Studio

A FastAPI web UI on a single graph canvas:

- **Lenses** — Topology · Coverage · Validation · Diff · Sensitivity
- **Right-click actions** — approve, rename, create relationships, mask a field
- **Helpers** — Auto-Map, Suggest FKs, naming conventions, denorm analysis

Every edit is a **draft**. Nothing hits the database without an explicit save.

---

## 3 · Move the data — three paths

1. **Batch** — generate `arangoimport` scripts (topological order)
2. **Stream** — direct HTTP bulk load, server-side cursors, parallel workers, `--dry-run`
3. **CDC** — logical replication + Kafka/Debezium, configurable conflict policies

Plus retry/backoff, progress + throughput, per-row error reporting, incremental `--since`.

---

## 4 · Govern what you migrate

- **Catalog classifications** (OpenMetadata) pulled at snapshot time
- **Sensitivity gate** at load — above-threshold fields excluded by default
- **Transform-at-load masking** — hash / tokenize / redact / nullify
- **Emitted enforcement artifacts** — classification manifest, RBAC grants, OPA/Rego stub

*r2g advises; the serving layer enforces.*

---

## 5 · The LLM proposes, the pipeline disposes

Optional AI ontology assistant:

- The LLM **never writes** to the graph — its output flows through the same validate → review → load path
- **Hallucination-proof**: every suggestion validated against the real schema; worst case degrades to the mechanical mapping
- **Metadata-only, PII-redacted, per-item human review, temperature 0**

---

## 6 · Federate — query in place (new)

The same mapping exports as **two load-independent artifacts**:

- **`export-csi`** → **CSI v1**: conceptual model (CC-12 OWL naming) + ArangoDB physical mapping → the fabric's Arango leg (`arango-sparql-py`)
- **`export-r2rml`** → **R2RML**: the relational leg → Ontop answers SPARQL over the *live* database

No ETL. No copy. r2g is the **forward mapping producer** for the Contextual Data Fabric.

---

## 7 · The joins are the hard part — P6.7

Two sources share a business key — `account_id` in a ClickHouse usage table **and** in the Postgres CRM `Account` — but nothing declares they're the same thing.

**Cross-source shared-key inference (P6.7):**

- Proposes keys by **name + type compatibility + value overlap**
- Confirm-to-accept — **never invents topology**
- Emitted as `conceptualModel.joinKeys` → the executor **bind-joins**, no materialized edge

---

## 8 · One query, three engines, zero movement

```
        ┌─ PostgreSQL  (via Ontop)         ─┐
SPARQL ─┼─ ClickHouse  (native BGP→SQL)     ─┼─▶  grounded, cited answer
        └─ ArangoDB    (via arango-sparql-py)┘
                     joined on accountId (P6.7)
```

A single conceptual query fans out, joins on a **declared** cross-source key, and returns **grounded and cited** — no data moved.

---

## The philosophy

1. **Determinism first** — a mechanical baseline underpins everything; clever features *advise*
2. **Human-in-the-loop** — drafts, diffs, reviews; nothing lands without a save
3. **Safety & honesty** — referential-integrity checks, classification-aware egress, and a candid "reference implementation, not production-hardened" README

---

<!-- _paginate: false -->

## Get started

```bash
pipx install 'r2g-arango[postgres,ui]'
r2g source add shop postgresql "$PG_CONN"
r2g source snapshot shop
r2g ui        # open the Mapping Studio

# federate instead of migrate:
r2g export-csi   --config mapping.yaml --schema schema.json --output shop.csi.json
r2g export-r2rml --config mapping.yaml --schema schema.json --output shop.r2rml.ttl
```

**GitHub** `ArthurKeen/r2g-arango` · **PyPI** `r2g-arango` · **License** Apache 2.0

> An educational, experimental reference implementation — not production-hardened software.
