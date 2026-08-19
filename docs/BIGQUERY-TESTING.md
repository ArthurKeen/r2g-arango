# Testing r2g with BigQuery public datasets

A free, no-credit-card way to get **realistic, normalized relational schemas**
(real foreign keys, join tables, many-to-one/many-to-many relationships) to
exercise r2g's relational→graph mapping and FK inference.

> **Read this first — how it connects to r2g.** r2g does **not** have a BigQuery
> source connector today. Its source types are `postgresql`, `mysql`,
> `sqlserver`, `snowflake`, `csv`, and `kafka` (see `add-source`). So BigQuery is
> used here as a **source of test data**, not a live source. The flow is:
>
> ```
> BigQuery Sandbox  ──►  export a bounded subset to CSV (or load into Postgres)  ──►  r2g
> ```
>
> A native BigQuery connector is future work (see
> [`internal/PLAN-external-data-catalogs.md`](internal/PLAN-external-data-catalogs.md)).

---

## 1. Create a free BigQuery Sandbox (no credit card)

The **BigQuery Sandbox** gives you BigQuery's SQL engine, the `bq` CLI, and all
public datasets with **no billing account and no credit card**.

1. Go to <https://console.cloud.google.com/> and sign in with any Google account.
2. Create a new project (top bar → project picker → **New Project**).
3. When prompted to set up billing, **skip it** — you'll land in Sandbox mode.
4. Open **BigQuery** from the console menu. You can now query
   `bigquery-public-data.*` immediately.

**Sandbox limits (no payment method required):**

| Limit | Value |
|---|---|
| Query processing | **1 TiB / month** free (billed by *bytes scanned*, see §5) |
| Storage (tables you create) | **10 GiB** free |
| Table lifetime | Tables/partitions **auto-expire after 60 days** |
| Billing-only features | Unavailable in Sandbox — notably **Cloud Storage exports** (`bq extract` needs a bucket → needs billing), streaming inserts, and the Data Transfer Service |

The 60-day expiry and the no-GCS-export constraint are why the recommended path
below is "query a bounded subset straight to a local CSV," not "extract whole
tables."

---

## 2. Install the `bq` CLI

```bash
# macOS
brew install --cask google-cloud-sdk
# or: https://cloud.google.com/sdk/docs/install

gcloud init                     # pick your account + the Sandbox project
gcloud config set project YOUR_PROJECT_ID
```

---

## 3. Authenticate

Pick **one**. For local testing, prefer Application Default Credentials — no
long-lived key file on disk.

### Recommended: Application Default Credentials (ADC)

```bash
gcloud auth application-default login
```

The `bq` CLI and Google client libraries pick these up automatically. Nothing to
store, nothing to leak.

### Alternative: service-account JSON key (headless / CI)

Only if you can't use ADC (e.g. an unattended CI runner):

1. **IAM & Admin → Service Accounts → Create service account.**
2. Grant it the **BigQuery User** role (`roles/bigquery.user`) — enough to run
   jobs and read public data. (The narrower `roles/bigquery.jobUser` also works
   if you only run queries.)
3. Open the account → **Keys → Add key → Create new key → JSON**. Download it.
4. Point tools at it:

   ```bash
   export GOOGLE_APPLICATION_CREDENTIALS="/absolute/path/to/key.json"
   ```

**Cautions for the key path:**
- Google **discourages downloaded keys** (they're long-lived secrets). Use ADC
  where you can.
- Your org may block key creation via the `iam.disableServiceAccountKeyCreation`
  policy — if "Create key" is greyed out, that's why; use ADC.
- **Never commit the key.** r2g auto-loads `.env` from the working directory, so
  put `GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json` in `.env` (already
  gitignored) — not in tracked files.

---

## 4. Pick a dataset (relational ones are best for r2g)

Google hosts 200+ datasets under the `bigquery-public-data` project. For
**relational→graph** testing you want ones with real normalized tables and
foreign keys, not a single wide table:

| Dataset | Why it's good for r2g |
|---|---|
| **`bigquery-public-data.thelook_ecommerce`** | **Best starter.** Cleanly normalized: `users`, `orders`, `order_items`, `products`, `inventory_items`, `distribution_centers` — obvious FKs (`orders.user_id → users.id`, `order_items.order_id → orders.order_id`) and an order↔product join through `order_items`. Ideal for FK inference + join-table detection. |
| `bigquery-public-data.stackoverflow` | Users, posts, comments, votes, badges — rich cross-entity relationships. Large; filter hard. |
| `bigquery-public-data.github_repos` | Repos, commits, files, languages — big graph-shaped data. Very large; sample only. |
| `bigquery-public-data.imdb` | `title_basics`, `name_basics`, `title_ratings`, `title_principals` — people↔titles many-to-many. |
| `bigquery-public-data.new_york_taxi_trips` | Great for **volume/scale** testing, but denormalized (one wide table) — not a good relational-structure test. |

**Location gotcha:** public datasets live in the **`US`** multi-region. Run your
jobs — and create any of your own datasets you want to join against — in `US`,
or queries will fail with a location mismatch.

---

## 5. Explore cheaply — BigQuery bills by *bytes scanned*

BigQuery charges by **data scanned, not rows or time**, so `SELECT *` on a big
table can burn a chunk of your 1 TiB in one query. Guardrails:

```bash
# FREE — metadata only, no scan:
bq show --schema --format=prettyjson bigquery-public-data:thelook_ecommerce.orders
bq head -n 20 bigquery-public-data:thelook_ecommerce.orders      # sample rows, free

# See bytes BEFORE running (no charge):
bq query --use_legacy_sql=false --dry_run \
  'SELECT * FROM `bigquery-public-data.thelook_ecommerce.order_items`'

# HARD CAP the scan so a mistake can't run away:
bq query --use_legacy_sql=false --maximum_bytes_billed=1000000000 \
  'SELECT order_id, user_id, status FROM `bigquery-public-data.thelook_ecommerce.orders` LIMIT 1000'
```

Rules of thumb: never `SELECT *` on a large table; select only the columns you
need; filter on partitioned columns where present; use the **Preview** tab in the
console (free) to eyeball data.

---

## 6. Get the data into r2g

### Path A — query a bounded subset to CSV (Sandbox-friendly, recommended)

`bq extract` needs Cloud Storage (billing), so in Sandbox just stream a bounded
query result to a local CSV. Keep each table small enough to preserve the FK
relationships you care about (grab parents + a consistent slice of children):

```bash
mkdir -p thelook && cd thelook

for t in users orders order_items products distribution_centers; do
  bq query --use_legacy_sql=false --format=csv --max_rows=50000 \
    "SELECT * FROM \`bigquery-public-data.thelook_ecommerce.$t\` LIMIT 50000" \
    > "$t.csv"
done
```

Then register a **`csv`** source in r2g and generate a mapping. Exact commands
depend on your r2g version — see `r2g --help` / the top-level
[`README.md`](../README.md); conceptually:

```bash
r2g add-source thelook --type csv --path ./thelook   # register the CSV folder
r2g introspect thelook                               # discover tables/columns
r2g map thelook                                       # propose the graph mapping
```

> CSV loses declared keys and types, so r2g's **FK inference** does more of the
> work here — which is exactly what you're testing. For a stricter test that
> preserves keys, use Path B.

### Path B — load into Postgres (preserves types/keys)

Load the CSVs into a local Postgres (r2g's primary source), then point r2g at it:

```bash
# in r2g's .env (auto-loaded, gitignored):
PG_CONN=postgresql://user:password@localhost:5432/thelook
```

```bash
r2g add-source thelook --type postgresql --conn "$PG_CONN"
r2g introspect thelook && r2g map thelook
```

This gives r2g the real column types and any declared constraints, so you can
compare inferred-vs-declared relationships (the same comparison the
`schema-analyzer` FK engine does against the Chinook fixture).

---

## 7. Cost & safety recap

- Sandbox is **free, no credit card**; **1 TiB/month** query, **10 GiB** storage.
- You are billed by **bytes scanned** — `--dry_run` first, `--maximum_bytes_billed`
  to cap, never `SELECT *` on big tables.
- Public data is in **`US`** — keep jobs/datasets in `US`.
- Tables you create **expire after 60 days** in Sandbox.
- Keep any service-account key **out of git** (`.env` only); prefer ADC.

---

## Appendix — quickest possible smoke test

```bash
gcloud auth application-default login
bq head -n 5 bigquery-public-data:thelook_ecommerce.orders     # free, proves access
bq query --use_legacy_sql=false --maximum_bytes_billed=200000000 \
  'SELECT status, COUNT(*) c FROM `bigquery-public-data.thelook_ecommerce.orders` GROUP BY status ORDER BY c DESC'
```

If those two return rows, your Sandbox + auth are working and you can proceed to
§6.
