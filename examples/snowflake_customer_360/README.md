# Snowflake Customer 360 demo

This example creates a small, deterministic, fictional Customer 360 data set for
the r2g Studio. It uses the same key-pair environment variables as Contextual
Data Fabric and never reads private-key bytes itself.

## Required environment

The runtime connection is read-only:

```text
SNOWFLAKE_ACCOUNT
SNOWFLAKE_USER
SNOWFLAKE_PRIVATE_KEY_FILE
SNOWFLAKE_PRIVATE_KEY_FILE_PWD  # only for an encrypted key
SNOWFLAKE_WAREHOUSE
SNOWFLAKE_ROLE                  # read-only runtime role (for example CDF_RO)
```

Bootstrap additionally requires an explicitly named setup role:

```text
R2G_SNOWFLAKE_SETUP_ROLE=<role-with-create-schema>
```

Optional location overrides default to `CDF_FORGE.R2G_CUSTOMER_360`:

```text
R2G_SNOWFLAKE_DEMO_DATABASE=CDF_FORGE
R2G_SNOWFLAKE_DEMO_SCHEMA=R2G_CUSTOMER_360
```

The script rejects unsafe database and schema identifiers.

## Bootstrap and validate

From the r2g repository:

```bash
python examples/snowflake_customer_360/bootstrap.py
```

The script:

1. verifies the existing read-only role with `SELECT CURRENT_VERSION()`;
2. creates/replaces five tables under the dedicated demo schema using the setup
   role;
3. loads deterministic healthy, at-risk, and churn-risk scenarios;
4. grants the runtime role usage/select access;
5. validates key uniqueness/non-nullness and every child-to-parent relationship.

Snowflake tables intentionally declare no PK/FK constraints. The bundled JSON
file contains reviewed key declarations. RSA applies that overlay during
snapshot capture and marks each resulting FK model `enforced=false`; the JSON
file itself does not store an `enforced` field.

To validate without reseeding:

```bash
python examples/snowflake_customer_360/bootstrap.py --validate-only
```

## Studio demo rehearsal

1. Start `r2g ui`, create/select a non-`_system` ArangoDB target, and choose
   **+ New source → Use Customer 360 Snowflake demo**.
2. Confirm the workspace shows five vertex mappings and six curated FK edges.
   Their provenance is the bundled RSA overlay; Snowflake still enforces none
   of these constraints.
3. Open **Actions → View arangoimport script bundle**. Show the JSONL bundle,
   Documents, Edges, and Graph creation views.
4. Right-click a target collection and choose **View arangoimport command**;
   then do the same for a curated FK edge.
5. Switch to **CSV-direct** to show the alternative commands. Generate the
   required local CSVs first with `r2g source dump
   snowflake_customer_360 --output-dir ./dumps` before actually running that
   script. JSONL mode likewise expects transformed files under `./output`.
   Both defaults can be changed in the viewer's **Artifact directory** field.

The viewer only renders, copies, or downloads a secret-safe batch artifact.
The normal Studio **Load** action does not execute it; Load streams directly
from Snowflake to ArangoDB over HTTP. The generated script reads the target
password from `ARANGO_PASSWORD` at runtime and contains no Snowflake URL,
private-key path, or passphrase.

## Privilege remediation

If the setup role cannot create the schema, an administrator can grant the
equivalent narrow privileges. Substitute your database, setup role, runtime
role, and user names; the following are examples matching the defaults:

```sql
GRANT USAGE ON DATABASE CDF_FORGE TO ROLE CDF_FORGE;
GRANT CREATE SCHEMA ON DATABASE CDF_FORGE TO ROLE CDF_FORGE;
GRANT ROLE CDF_RO TO USER <SNOWFLAKE_USER>;
```

The configured setup role must be usable by `SNOWFLAKE_USER`. The bootstrap
owns the demo schema and can therefore grant:

```sql
GRANT USAGE ON SCHEMA CDF_FORGE.R2G_CUSTOMER_360 TO ROLE CDF_RO;
GRANT SELECT ON ALL TABLES IN SCHEMA CDF_FORGE.R2G_CUSTOMER_360 TO ROLE CDF_RO;
GRANT SELECT ON FUTURE TABLES IN SCHEMA CDF_FORGE.R2G_CUSTOMER_360 TO ROLE CDF_RO;
```

## Cost and cleanup

The data set contains four accounts and a few dozen related rows. Queries are
small, but Snowflake warehouse minimum billing still applies. Suspend the
warehouse after rehearsal if auto-suspend is not already configured.

Cleanup is explicit so a rehearsed schema remains available until removed:

```sql
DROP SCHEMA IF EXISTS CDF_FORGE.R2G_CUSTOMER_360;
```

