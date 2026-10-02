"""Bundled metadata for the Snowflake Customer 360 demo."""

from __future__ import annotations

import json
import os
import re
from importlib.resources import files
from typing import Any

DEMO_ID = "snowflake-customer-360"
SOURCE_NAME = "snowflake_customer_360"
PROJECT_NAME = "snowflake_customer_360"
DEFAULT_DATABASE = "CDF_FORGE"
DEFAULT_SCHEMA = "R2G_CUSTOMER_360"
OVERLAY_SOURCE = f"bundled:{DEMO_ID}/keys.overlay.json"

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]{0,254}")


def validate_identifier(label: str, value: str) -> str:
    """Validate and normalize one unquoted Snowflake identifier."""
    value = value.strip()
    if not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(
            f"Invalid Snowflake demo {label} identifier; use letters, "
            "digits, underscore, or dollar sign and start with a letter/underscore"
        )
    return value.upper()


def configured_location() -> tuple[str, str]:
    """Return validated, unquoted Snowflake database/schema identifiers."""
    database = os.environ.get(
        "R2G_SNOWFLAKE_DEMO_DATABASE", DEFAULT_DATABASE
    ).strip()
    schema = os.environ.get(
        "R2G_SNOWFLAKE_DEMO_SCHEMA", DEFAULT_SCHEMA
    ).strip()
    return (
        validate_identifier("database", database),
        validate_identifier("schema", schema),
    )


def connection_string() -> str:
    """Build the env-reference URI persisted by the Studio preset."""
    database, schema = configured_location()
    uri = (
        f"snowflake://$SNOWFLAKE_USER:@$SNOWFLAKE_ACCOUNT/{database}/{schema}"
        "?warehouse=$SNOWFLAKE_WAREHOUSE"
        "&role=$SNOWFLAKE_ROLE"
        "&private_key_file=$SNOWFLAKE_PRIVATE_KEY_FILE"
    )
    if os.environ.get("SNOWFLAKE_PRIVATE_KEY_FILE_PWD"):
        uri += "&private_key_file_pwd=$SNOWFLAKE_PRIVATE_KEY_FILE_PWD"
    return uri


def load_overlay() -> dict[str, Any]:
    """Load the packaged, reviewed key overlay."""
    resource = files(__package__).joinpath("keys.overlay.json")
    return json.loads(resource.read_text(encoding="utf-8"))


__all__ = [
    "DEFAULT_DATABASE",
    "DEFAULT_SCHEMA",
    "DEMO_ID",
    "OVERLAY_SOURCE",
    "PROJECT_NAME",
    "SOURCE_NAME",
    "configured_location",
    "connection_string",
    "load_overlay",
    "validate_identifier",
]
