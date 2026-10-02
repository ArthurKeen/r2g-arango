"""Experiment 2 — write once (Postgres dialect), transpile with SQLGlot, run everywhere."""
import json, sys, sqlglot, psycopg2, duckdb, clickhouse_connect
from connections import PG_CONN, CLICKHOUSE_DSN
pg = psycopg2.connect(PG_CONN); pg.autocommit = True
dk = duckdb.connect()
ch = clickhouse_connect.get_client(dsn=CLICKHOUSE_DSN)
print("sqlglot", sqlglot.__version__)
names = sorted(sqlglot.dialects.DIALECTS) if hasattr(sqlglot.dialects, "DIALECTS") else sorted(sqlglot.Dialect.classes)
print("dialects:", len(names), "| has flink:", any("flink" in d.lower() for d in names),
      "| has ksql:", any("ksql" in d.lower() for d in names), "| has aql:", any(d.lower()=="aql" for d in names))

CANON = {
 "T1 null in concatenation":   "SELECT UPPER(CAST(NULL AS TEXT)) || ' ' || 'Smith'",
 "T2 first non-null":          "SELECT COALESCE('', CAST(NULL AS TEXT), 'unknown')",
 "T3 bad text to number":      "SELECT CAST('12a' AS INTEGER)",
 "T4 band for NULL score":     "SELECT CASE WHEN CAST(NULL AS INTEGER) < 30 THEN 'low' WHEN CAST(NULL AS INTEGER) < 70 THEN 'medium' ELSE 'high' END",
 "T5 composite key, float id": "SELECT CAST(7 AS TEXT) || '_' || CAST(CAST(42.0 AS DOUBLE PRECISION) AS TEXT)",
 "T7 first 3 chars of Zürich": "SELECT SUBSTRING('Zürich', 1, 3)",
 "T8 integer division":        "SELECT 7 / 2",
}
def ex(engine, sql):
    try:
        if engine == "postgres":
            c = pg.cursor(); c.execute(sql); return repr(c.fetchone()[0])
        if engine == "duckdb":   return repr(dk.execute(sql).fetchone()[0])
        if engine == "clickhouse": return repr(ch.query(sql).result_rows[0][0])
    except Exception as e:
        return "ERROR " + type(e).__name__
out = {}
for name, sql in CANON.items():
    ref = ex("postgres", sql)
    print(f"\n{name}\n  postgres (written once)  {sql[7:]}\n    -> {ref}")
    row = {"canonical": sql, "postgres": ref, "targets": {}}
    for tgt in ("duckdb", "clickhouse"):
        t = sqlglot.transpile(sql, read="postgres", write=tgt)[0]
        r = ex(tgt, t)
        # Compare outcomes, not text: two engines that both refuse agree, even
        # though their exception classes differ.
        both_err = r.startswith("ERROR ") and ref.startswith("ERROR ")
        verdict = "same" if both_err or r == ref else "DIFFERENT"
        print(f"  {tgt:<11} {verdict:<9}  {t[7:]}\n    -> {r}")
        row["targets"][tgt] = {"sql": t, "result": r, "verdict": verdict}
    for tgt in ("tsql", "snowflake", "mysql", "spark"):
        row["targets"][tgt] = {"sql": sqlglot.transpile(sql, read="postgres", write=tgt)[0]}
    out[name] = row
json.dump(out, open(sys.argv[1], "w"), indent=1)
print("\n--- same canonical T1 and T8, rendered for dialects we cannot execute here ---")
for n in ("T1 null in concatenation", "T8 integer division"):
    for tgt in ("tsql", "snowflake", "mysql", "spark"):
        print(f"  {n[:2]} {tgt:<10} {out[n]['targets'][tgt]['sql']}")
