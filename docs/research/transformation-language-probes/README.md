# Transformation-language probes

Reproduces the measurements in `../transformation-language-evaluation.md`.

    python -m venv .venv && .venv/bin/pip install sqlglot duckdb psycopg2-binary \
        clickhouse-connect python-arango cel-expr-python
    docker compose up -d postgres clickhouse arangodb     # r2g's test services
    .venv/bin/python probe_engines.py engines.json        # experiment 1
    .venv/bin/python probe_sqlglot.py sqlglot.json        # experiment 2

Measured 2026-09-29 with: PostgreSQL 16, ClickHouse 24.8, ArangoDB 3.12.9,
DuckDB 1.5.6, SQLGlot 30.20.0, cel-expr-python 0.1.3 (Python 3.11, macOS arm64).
Connection defaults are the r2g integration-test defaults in
`tests/integration/conftest.py`. `probe_engines.py` expects an ArangoDB database
named `lpg_perf`; any database works — change the name on the `adb =` line.
DuckDB renders timestamps in the host's time zone, so probe P6 varies by machine.
