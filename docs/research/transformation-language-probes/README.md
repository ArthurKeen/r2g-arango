# Transformation-language probes

Reproduces the measurements in `../transformation-language-evaluation.md`.

    python -m venv .venv && .venv/bin/pip install sqlglot duckdb psycopg2-binary \
        clickhouse-connect python-arango cel-expr-python python-dotenv -e ../../..
    docker compose up -d postgres clickhouse arangodb     # r2g's test services
    .venv/bin/python probe_engines.py engines.json        # experiment 1 (section 4.1)
    .venv/bin/python probe_sqlglot.py sqlglot.json        # experiment 2 (section 4.2)
    .venv/bin/python probe_delegation.py delegation.json  # r2g local vs delegation (section 5.1)

Measured 2026-09-29, re-run 2026-10-02, with: PostgreSQL 16, ClickHouse 24.8,
ArangoDB 3.12.9, DuckDB 1.5.6, SQLGlot 30.20.0, cel-expr-python 0.1.3
(Python 3.11, macOS arm64).

Connections (`connections.py`) read the same variables, defaults and repo-root
`.env` as `tests/integration/conftest.py`: `PG_CONN`, `CLICKHOUSE_DSN`,
`ARANGO_ENDPOINT`, `ARANGO_USER`, `ARANGO_PASSWORD`. `ARANGO_DB` (default
`_system`) picks the ArangoDB database; a missing database stops the run at
start-up rather than showing up as an engine error in every row.

Probe P6 depends on the environment: Postgres and DuckDB render timestamps in
the session time zone — Postgres takes the server's `TimeZone` setting, DuckDB
the host's. Both settings are printed with the P6 results.
