"""RESERVED_COLUMN_NAMES must stay in step with the engines it speaks for.

The first version of this list was written from memory and validated against
itself — it probed only the words it already contained, so no probe could fail.
It was wrong in both directions: it accepted `current_date`, `array`, `do`,
`returning` and `ilike` (all rejected by live Postgres) and refused `key`,
`range`, `session` and `filter` (accepted by Postgres and ClickHouse both).

These tests probe the live engines instead, so the list cannot silently drift
from them. Regenerate with `python scripts/derive_reserved_words.py`.
"""

from __future__ import annotations

import pytest

from r2g.forge.core import RESERVED_COLUMN_NAMES

from .conftest import CLICKHOUSE_DSN, PG_CONN, requires_clickhouse, requires_pg


def _pg_rejects(conn, word: str) -> bool:
    try:
        conn.execute(f"CREATE TEMP TABLE _probe (id int, {word} text)")
        conn.execute("DROP TABLE _probe")
        return False
    except Exception:
        return True


@requires_pg
def test_no_word_postgres_rejects_is_missing_from_the_list():
    """The direction that ships broken DDL: a name the forge accepts and the
    engine refuses fails at load, which is exactly what F-7 exists to prevent."""
    import psycopg

    missing = []
    with psycopg.connect(PG_CONN, autocommit=True) as conn:
        words = [r[0] for r in conn.execute("SELECT word FROM pg_get_keywords()").fetchall()]
        for w in words:
            if not w.isidentifier():
                continue
            if _pg_rejects(conn, w) and w.lower() not in RESERVED_COLUMN_NAMES:
                missing.append(w.lower())
    assert not missing, (
        f"Postgres rejects these as column names but the forge accepts them: "
        f"{sorted(missing)} — regenerate with scripts/derive_reserved_words.py"
    )


@requires_pg
@requires_clickhouse
@pytest.mark.parametrize("word", ["key", "range", "session", "result", "filter", "partition"])
def test_words_both_engines_accept_are_not_refused(word):
    """The other direction: over-refusal rejects ontologies that would have
    generated valid DDL, and `key` is an ordinary property name."""
    import clickhouse_connect
    import psycopg

    with psycopg.connect(PG_CONN, autocommit=True) as conn:
        assert not _pg_rejects(conn, word), f"premise changed: Postgres now rejects {word!r}"
    client = clickhouse_connect.get_client(dsn=CLICKHOUSE_DSN)
    try:
        client.command(f"CREATE TABLE _probe (id Int64, {word} String) ENGINE = MergeTree ORDER BY (id)")
        client.command("DROP TABLE _probe")
    finally:
        client.close()
    assert word not in RESERVED_COLUMN_NAMES, (
        f"{word!r} is accepted by both Postgres and ClickHouse but the forge refuses it"
    )
