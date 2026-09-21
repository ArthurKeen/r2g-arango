#!/usr/bin/env python3
"""Derive RESERVED_COLUMN_NAMES from what the target engines actually reject.

The first version of this list was written from memory as "the SQL:2016
reserved words" and was wrong in both directions: it accepted `current_date`,
`array`, `do`, `returning` and `ilike` (all rejected by live Postgres) while
refusing `key`, `range`, `session` and `filter` (all accepted by Postgres AND
ClickHouse). Its validation probed only the words already in the list, so the
check could not fail.

This probes instead. Candidates come from Postgres's own keyword catalogue
(`pg_get_keywords()`, ~470 entries, authoritative), each is tried as a real
column name against live Postgres and live ClickHouse, and the union of what
they reject is combined with Snowflake's documented reserved words.

    python scripts/derive_reserved_words.py          # print the frozenset body

Re-run when a target engine is upgraded. `tests/integration/
test_reserved_words.py` re-probes and fails if the checked-in list has drifted
from the live engines, so this file is a generator, not a source of truth.
"""
from __future__ import annotations

import os
import sys

# Snowflake reserved words that cannot be an unquoted column name.
# Source: Snowflake docs, "Reserved & Limited Keywords". Kept as a literal
# because bulk-probing a real account costs credits per statement; the
# integration test probes only the handful the fixture ontology generates.
SNOWFLAKE_RESERVED = {
    "account", "all", "alter", "and", "any", "as", "between", "by", "case",
    "cast", "check", "column", "connect", "connection", "constraint", "create",
    "cross", "current", "current_date", "current_time", "current_timestamp",
    "current_user", "database", "delete", "distinct", "drop", "else", "exists",
    "false", "following", "for", "from", "full", "grant", "group", "gscluster",
    "having", "ilike", "in", "increment", "inner", "insert", "intersect",
    "into", "is", "issue", "join", "lateral", "left", "like", "localtime",
    "localtimestamp", "minus", "natural", "not", "null", "of", "on", "or",
    "order", "organization", "qualify", "regexp", "revoke", "right", "rlike",
    "row", "rows", "sample", "schema", "select", "set", "some", "start",
    "table", "tablesample", "then", "to", "trigger", "true", "try_cast",
    "union", "unique", "update", "using", "values", "view", "when",
    "whenever", "where", "with",
}

PG_DSN = os.getenv("PG_CONN", "postgresql://r2g:r2g_test_2026@localhost:5432/northwind")
CH_DSN = os.getenv("CLICKHOUSE_DSN", "clickhouse://r2g:r2g_test_2026@localhost:8124/forge")


def pg_candidates_and_rejects():
    import psycopg

    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        words = [r[0] for r in conn.execute("SELECT word FROM pg_get_keywords()").fetchall()]
        rejected = set()
        for w in words:
            if not w.isidentifier():
                continue
            try:
                conn.execute(f"CREATE TEMP TABLE _probe (id int, {w} text)")
                conn.execute("DROP TABLE _probe")
            except Exception:
                rejected.add(w.lower())
        return [w.lower() for w in words], rejected


def ch_rejects(candidates):
    import clickhouse_connect

    client = clickhouse_connect.get_client(dsn=CH_DSN)
    rejected = set()
    try:
        for w in candidates:
            try:
                client.command(
                    f"CREATE TABLE _probe (id Int64, {w} String) ENGINE = MergeTree ORDER BY (id)"
                )
                client.command("DROP TABLE _probe")
            except Exception:
                rejected.add(w)
                try:
                    client.command("DROP TABLE IF EXISTS _probe")
                except Exception:
                    pass
    finally:
        client.close()
    return rejected


def main() -> int:
    candidates, pg = pg_candidates_and_rejects()
    ch = ch_rejects(candidates)
    union = sorted(pg | ch | SNOWFLAKE_RESERVED)
    print(f"# candidates probed: {len(candidates)}", file=sys.stderr)
    print(f"# postgres rejects : {len(pg)}", file=sys.stderr)
    print(f"# clickhouse rejects: {len(ch)}  (extra over pg: {sorted(ch - pg)})", file=sys.stderr)
    print(f"# snowflake documented: {len(SNOWFLAKE_RESERVED)}", file=sys.stderr)
    print(f"# union: {len(union)}", file=sys.stderr)
    for i in range(0, len(union), 6):
        print("    " + " ".join(union[i : i + 6]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
