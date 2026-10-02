"""Connection settings shared by the probes.

Same variables and defaults as ``tests/integration/conftest.py`` (including the
repo-root ``.env``), so a stack configured for the integration tests is
configured for the probes too. ``ARANGO_DB`` is probe-only; ``_system`` always
exists, so a fresh ``docker compose up`` needs nothing else.
"""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=False)
except ImportError:
    pass

PG_CONN = os.getenv("PG_CONN", "postgresql://r2g:r2g_test_2026@localhost:5432/northwind")
CLICKHOUSE_DSN = os.getenv("CLICKHOUSE_DSN", "clickhouse://r2g:r2g_test_2026@localhost:8124/forge")
ARANGO_ENDPOINT = os.getenv("ARANGO_ENDPOINT", "http://localhost:8540")
ARANGO_USER = os.getenv("ARANGO_USER", "root")
ARANGO_PASSWORD = os.getenv("ARANGO_PASSWORD", "r2g_test_2026")
ARANGO_DB = os.getenv("ARANGO_DB", "_system")
