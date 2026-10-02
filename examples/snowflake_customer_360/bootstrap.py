#!/usr/bin/env python3
"""Create deterministic, constraint-free Customer 360 demo data in Snowflake."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterable
from typing import Any

from dotenv import load_dotenv

from r2g.connectors.base import expand_env_vars
from r2g.connectors.snowflake import _load_snowflake_connector, _parse_snowflake_url
from r2g.demo.snowflake_customer_360 import (
    configured_location,
    connection_string,
    validate_identifier,
)
from r2g.security import scrub_dsn_credentials

TABLE_DDL = {
    "ACCOUNTS": """
        CREATE OR REPLACE TABLE {table} (
          ACCOUNT_ID VARCHAR NOT NULL,
          ACCOUNT_NAME VARCHAR NOT NULL,
          DOMAIN VARCHAR NOT NULL,
          SEGMENT VARCHAR NOT NULL,
          ARR NUMBER(12, 2) NOT NULL,
          RENEWAL_DATE DATE NOT NULL,
          HEALTH_STATUS VARCHAR NOT NULL,
          CREATED_AT TIMESTAMP_NTZ NOT NULL
        )
    """,
    "CONTACTS": """
        CREATE OR REPLACE TABLE {table} (
          CONTACT_ID VARCHAR NOT NULL,
          ACCOUNT_ID VARCHAR NOT NULL,
          FULL_NAME VARCHAR NOT NULL,
          EMAIL VARCHAR NOT NULL,
          ROLE VARCHAR NOT NULL,
          IS_CHAMPION BOOLEAN NOT NULL
        )
    """,
    "EMAIL_EVENTS": """
        CREATE OR REPLACE TABLE {table} (
          EMAIL_EVENT_ID VARCHAR NOT NULL,
          CONTACT_ID VARCHAR NOT NULL,
          ACCOUNT_ID VARCHAR NOT NULL,
          EVENT_TYPE VARCHAR NOT NULL,
          EVENT_AT TIMESTAMP_NTZ NOT NULL,
          SUBJECT VARCHAR NOT NULL
        )
    """,
    "ZOOM_TELEMETRY": """
        CREATE OR REPLACE TABLE {table} (
          MEETING_ID VARCHAR NOT NULL,
          CONTACT_ID VARCHAR NOT NULL,
          ACCOUNT_ID VARCHAR NOT NULL,
          STARTED_AT TIMESTAMP_NTZ NOT NULL,
          DURATION_MINUTES NUMBER NOT NULL,
          ATTENDED BOOLEAN NOT NULL,
          TOPIC VARCHAR NOT NULL
        )
    """,
    "HEALTH_SIGNALS": """
        CREATE OR REPLACE TABLE {table} (
          SIGNAL_ID VARCHAR NOT NULL,
          ACCOUNT_ID VARCHAR NOT NULL,
          OBSERVED_AT TIMESTAMP_NTZ NOT NULL,
          SIGNAL_TYPE VARCHAR NOT NULL,
          SIGNAL_VALUE FLOAT NOT NULL,
          RISK_LEVEL VARCHAR NOT NULL,
          DETAIL VARCHAR NOT NULL
        )
    """,
}

ROWS: dict[str, list[tuple[Any, ...]]] = {
    "ACCOUNTS": [
        (
            "A100",
            "Northstar Labs",
            "northstar.example",
            "Enterprise",
            480000,
            "2027-03-31",
            "Healthy",
            "2024-02-01 09:00:00",
        ),
        (
            "A200",
            "Harbor Health",
            "harborhealth.example",
            "Enterprise",
            310000,
            "2026-12-15",
            "At Risk",
            "2024-05-12 09:00:00",
        ),
        (
            "A300",
            "Redwood Retail",
            "redwoodretail.example",
            "Mid-Market",
            185000,
            "2026-11-01",
            "Churn Risk",
            "2025-01-20 09:00:00",
        ),
        (
            "A400",
            "Summit Robotics",
            "summitrobotics.example",
            "Growth",
            95000,
            "2027-05-30",
            "Healthy",
            "2025-03-10 09:00:00",
        ),
    ],
    "CONTACTS": [
        ("C101", "A100", "Maya Chen", "maya@northstar.example", "VP Security", True),
        ("C102", "A100", "Theo Grant", "theo@northstar.example", "Security Architect", False),
        ("C201", "A200", "Priya Shah", "priya@harborhealth.example", "CIO", True),
        ("C202", "A200", "Owen Brooks", "owen@harborhealth.example", "IT Director", False),
        ("C301", "A300", "Elena Rossi", "elena@redwoodretail.example", "VP Infrastructure", False),
        ("C302", "A300", "Marcus Lee", "marcus@redwoodretail.example", "Procurement", False),
        ("C401", "A400", "Amina Yusuf", "amina@summitrobotics.example", "CTO", True),
        ("C402", "A400", "Jon Bell", "jon@summitrobotics.example", "Platform Lead", False),
    ],
    "EMAIL_EVENTS": [
        ("E1001", "C101", "A100", "REPLIED", "2026-09-28 15:10:00", "Quarterly value review"),
        ("E1002", "C102", "A100", "OPENED", "2026-09-29 11:20:00", "New capability briefing"),
        ("E2001", "C201", "A200", "OPENED", "2026-09-10 08:05:00", "Renewal planning"),
        ("E2002", "C202", "A200", "NO_RESPONSE", "2026-09-18 13:45:00", "Adoption workshop"),
        ("E3001", "C301", "A300", "NO_RESPONSE", "2026-08-20 10:00:00", "Executive check-in"),
        ("E3002", "C302", "A300", "BOUNCED", "2026-09-02 10:00:00", "Commercial review"),
        ("E4001", "C401", "A400", "REPLIED", "2026-09-30 16:30:00", "Expansion planning"),
        ("E4002", "C402", "A400", "REPLIED", "2026-09-30 17:00:00", "Technical workshop"),
    ],
    "ZOOM_TELEMETRY": [
        ("M1001", "C101", "A100", "2026-09-27 14:00:00", 52, True, "Quarterly business review"),
        ("M1002", "C102", "A100", "2026-09-29 16:00:00", 41, True, "Architecture workshop"),
        ("M2001", "C201", "A200", "2026-09-12 12:00:00", 24, True, "Renewal discovery"),
        ("M2002", "C202", "A200", "2026-09-25 12:00:00", 0, False, "Adoption recovery"),
        ("M3001", "C301", "A300", "2026-08-15 09:00:00", 0, False, "Executive alignment"),
        ("M3002", "C302", "A300", "2026-09-05 09:00:00", 0, False, "Commercial review"),
        ("M4001", "C401", "A400", "2026-09-26 10:00:00", 58, True, "Expansion roadmap"),
        ("M4002", "C402", "A400", "2026-09-30 10:00:00", 47, True, "Platform deep dive"),
    ],
    "HEALTH_SIGNALS": [
        ("S1001", "A100", "2026-08-31 00:00:00", "WEEKLY_ACTIVE_USERS", 820, "LOW", "Usage up 12% month over month"),
        ("S1002", "A100", "2026-09-30 00:00:00", "FEATURE_ADOPTION", 0.88, "LOW", "Broad feature adoption"),
        (
            "S2001",
            "A200",
            "2026-08-31 00:00:00",
            "WEEKLY_ACTIVE_USERS",
            390,
            "MEDIUM",
            "Usage down 18% month over month",
        ),
        ("S2002", "A200", "2026-09-30 00:00:00", "SUPPORT_ESCALATIONS", 3, "MEDIUM", "Three unresolved escalations"),
        ("S3001", "A300", "2026-08-31 00:00:00", "WEEKLY_ACTIVE_USERS", 74, "HIGH", "Usage down 61% month over month"),
        (
            "S3002",
            "A300",
            "2026-09-30 00:00:00",
            "EXECUTIVE_ENGAGEMENT",
            0,
            "HIGH",
            "No executive engagement in 60 days",
        ),
        ("S4001", "A400", "2026-08-31 00:00:00", "WEEKLY_ACTIVE_USERS", 245, "LOW", "Usage up 35% month over month"),
        ("S4002", "A400", "2026-09-30 00:00:00", "EXPANSION_INTEREST", 0.92, "LOW", "Two new teams in evaluation"),
    ],
}

PK_COLUMNS = {
    "ACCOUNTS": "ACCOUNT_ID",
    "CONTACTS": "CONTACT_ID",
    "EMAIL_EVENTS": "EMAIL_EVENT_ID",
    "ZOOM_TELEMETRY": "MEETING_ID",
    "HEALTH_SIGNALS": "SIGNAL_ID",
}

FK_CHECKS = [
    ("CONTACTS", "ACCOUNT_ID", "ACCOUNTS", "ACCOUNT_ID"),
    ("EMAIL_EVENTS", "CONTACT_ID", "CONTACTS", "CONTACT_ID"),
    ("EMAIL_EVENTS", "ACCOUNT_ID", "ACCOUNTS", "ACCOUNT_ID"),
    ("ZOOM_TELEMETRY", "CONTACT_ID", "CONTACTS", "CONTACT_ID"),
    ("ZOOM_TELEMETRY", "ACCOUNT_ID", "ACCOUNTS", "ACCOUNT_ID"),
    ("HEALTH_SIGNALS", "ACCOUNT_ID", "ACCOUNTS", "ACCOUNT_ID"),
]


def _qualified(database: str, schema: str, table: str) -> str:
    return f'"{database}"."{schema}"."{table}"'


def _connect_args(role: str) -> dict[str, Any]:
    params = _parse_snowflake_url(expand_env_vars(connection_string()))
    params.pop("_url_schema", None)
    _, schema = configured_location()
    params["schema"] = schema
    params["role"] = role
    return params


def _execute(cur: Any, sql: str, rows: Iterable[tuple[Any, ...]] | None = None) -> None:
    if rows is None:
        cur.execute(sql)
    else:
        cur.executemany(sql, list(rows))


def _validate(cur: Any, database: str, schema: str) -> None:
    for table, pk in PK_COLUMNS.items():
        q = _qualified(database, schema, table)
        cur.execute(f'SELECT COUNT(*), COUNT("{pk}"), COUNT(DISTINCT "{pk}") FROM {q}')
        total, non_null, distinct = cur.fetchone()
        if total != non_null or total != distinct:
            raise RuntimeError(
                f"{table}.{pk} is not a safe key: total={total}, non_null={non_null}, distinct={distinct}"
            )

    for child, child_col, parent, parent_col in FK_CHECKS:
        cq = _qualified(database, schema, child)
        pq = _qualified(database, schema, parent)
        cur.execute(
            f"SELECT COUNT(*) FROM {cq} c LEFT JOIN {pq} p "
            f'ON c."{child_col}" = p."{parent_col}" '
            f'WHERE c."{child_col}" IS NOT NULL AND p."{parent_col}" IS NULL'
        )
        (orphans,) = cur.fetchone()
        if orphans:
            raise RuntimeError(f"{child}.{child_col} -> {parent}.{parent_col} has {orphans} orphan row(s)")


def bootstrap(*, validate_only: bool = False) -> None:
    load_dotenv()
    database, schema = configured_location()
    setup_role = os.environ.get("R2G_SNOWFLAKE_SETUP_ROLE", "").strip()
    runtime_role = os.environ.get("SNOWFLAKE_ROLE", "").strip()
    if not setup_role:
        raise RuntimeError(
            "R2G_SNOWFLAKE_SETUP_ROLE is required. For the existing CDF Forge account, set it explicitly to CDF_FORGE."
        )
    if not runtime_role:
        raise RuntimeError("SNOWFLAKE_ROLE is required for the read-only runtime probe")
    setup_role = validate_identifier("setup role", setup_role)
    runtime_role = validate_identifier("runtime role", runtime_role)

    snowflake = _load_snowflake_connector()
    runtime = snowflake.connect(**_connect_args(runtime_role))
    try:
        cur = runtime.cursor()
        try:
            cur.execute("SELECT CURRENT_VERSION()")
            cur.fetchone()
        finally:
            cur.close()
    finally:
        runtime.close()
    print(f"Read-only Snowflake probe succeeded with role {runtime_role}.")

    grant_error: Exception | None = None
    setup = snowflake.connect(**_connect_args(setup_role))
    try:
        cur = setup.cursor()
        try:
            if not validate_only:
                cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{database}"."{schema}"')
                for table, ddl in TABLE_DDL.items():
                    cur.execute(ddl.format(table=_qualified(database, schema, table)))
                for table, rows in ROWS.items():
                    placeholders = ", ".join(["%s"] * len(rows[0]))
                    _execute(
                        cur,
                        f"INSERT INTO {_qualified(database, schema, table)} VALUES ({placeholders})",
                        rows,
                    )
            _validate(cur, database, schema)
            if not validate_only:
                try:
                    cur.execute(f'GRANT USAGE ON SCHEMA "{database}"."{schema}" TO ROLE "{runtime_role}"')
                    cur.execute(
                        f'GRANT SELECT ON ALL TABLES IN SCHEMA "{database}"."{schema}" TO ROLE "{runtime_role}"'
                    )
                    cur.execute(
                        f'GRANT SELECT ON FUTURE TABLES IN SCHEMA "{database}"."{schema}" TO ROLE "{runtime_role}"'
                    )
                except Exception as exc:  # noqa: BLE001 - verify existing grants below
                    grant_error = exc
        finally:
            cur.close()
    finally:
        setup.close()

    runtime = snowflake.connect(**_connect_args(runtime_role))
    try:
        cur = runtime.cursor()
        try:
            cur.execute(f"SELECT COUNT(*) FROM {_qualified(database, schema, 'ACCOUNTS')}")
            cur.fetchone()
        except Exception as exc:
            remediation = (
                f'GRANT USAGE ON DATABASE "{database}" TO ROLE "{runtime_role}"; '
                f'GRANT USAGE ON SCHEMA "{database}"."{schema}" '
                f'TO ROLE "{runtime_role}"; '
                f'GRANT SELECT ON ALL TABLES IN SCHEMA "{database}"."{schema}" '
                f'TO ROLE "{runtime_role}"; '
                f'GRANT SELECT ON FUTURE TABLES IN SCHEMA "{database}"."{schema}" '
                f'TO ROLE "{runtime_role}";'
            )
            raise RuntimeError(
                f"The demo data was created, but the runtime role cannot read it. Run as SECURITYADMIN: {remediation}"
            ) from exc
        finally:
            cur.close()
    finally:
        runtime.close()

    action = "validated" if validate_only else "created and validated"
    print(f"{database}.{schema} {action}; no PK/FK constraints were declared.")
    if grant_error is not None:
        print(
            "The setup role could not grant privileges directly; existing "
            f"future grants already make the schema readable by {runtime_role}."
        )


def _safe_operator_error(exc: Exception) -> str:
    message = scrub_dsn_credentials(str(exc))
    for name in (
        "SNOWFLAKE_PASSWORD",
        "SNOWFLAKE_PRIVATE_KEY_FILE",
        "SNOWFLAKE_PRIVATE_KEY_FILE_PWD",
    ):
        value = os.environ.get(name)
        if value:
            message = message.replace(value, "***")
    return message


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate an existing demo schema without reseeding it",
    )
    args = parser.parse_args()
    try:
        bootstrap(validate_only=args.validate_only)
    except Exception as exc:  # noqa: BLE001 - operator CLI needs one concise failure
        print(
            f"Customer 360 bootstrap failed: {_safe_operator_error(exc)}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
