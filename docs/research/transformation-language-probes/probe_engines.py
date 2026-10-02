"""Experiment 1 — does the same intent give the same answer on each engine?"""
import json, sys, datetime
import os; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "src"))
import psycopg2, duckdb, clickhouse_connect
from arango import ArangoClient
from r2g.expressions import evaluate as r2g_eval
from cel_expr_python import cel
from cel_expr_python.ext import ext_strings
from connections import PG_CONN, CLICKHOUSE_DSN, ARANGO_ENDPOINT, ARANGO_USER, ARANGO_PASSWORD, ARANGO_DB

pg = psycopg2.connect(PG_CONN); pg.autocommit = True
dk = duckdb.connect()
ch = clickhouse_connect.get_client(dsn=CLICKHOUSE_DSN)
adb = ArangoClient(hosts=ARANGO_ENDPOINT).db(ARANGO_DB, username=ARANGO_USER, password=ARANGO_PASSWORD)
adb.properties()  # fail here, loudly, rather than as an "engine error" in every ArangoDB row

class CelError(Exception):
    """CEL returns errors as values; surface them as errors like every other engine."""

def run(engine, expr, env=None):
    try:
        if engine == "postgres":
            c = pg.cursor(); c.execute("SELECT " + expr); v = c.fetchone()[0]
        elif engine == "duckdb":
            v = dk.execute("SELECT " + expr).fetchone()[0]
        elif engine == "clickhouse":
            v = ch.query("SELECT " + expr).result_rows[0][0]
        elif engine == "arangodb":
            v = next(adb.aql.execute("RETURN " + expr))
        elif engine == "r2g":
            v = r2g_eval(expr, env or {})
        elif engine == "python":
            v = eval(expr, {"datetime": datetime}, dict(env or {}))
        elif engine == "cel":
            e = cel.NewEnv(variables={k: cel.Type.DYN for k in (env or {})},
                           extensions=[ext_strings.ExtStrings()])
            r = e.compile(expr).eval(data=env or {})
            if r.type() == cel.Type.ERROR:
                raise CelError(r.value())
            v = r.value()
        if isinstance(v, (datetime.datetime, datetime.date)): v = v.isoformat()
        return {"ok": True, "value": v, "type": type(v).__name__}
    except Exception as ex:
        msg = str(ex).strip().splitlines()[0][:110]
        return {"ok": False, "error": type(ex).__name__ + ": " + msg}

P = {}
P["P1 null inside concatenation  (first=NULL, last='Smith')"] = [
  ("postgres",   "UPPER(NULL::text) || ' ' || 'Smith'", None, "|| operator"),
  ("postgres",   "CONCAT(UPPER(NULL::text), ' ', 'Smith')", None, "CONCAT()"),
  ("duckdb",     "UPPER(NULL::VARCHAR) || ' ' || 'Smith'", None, "|| operator"),
  ("duckdb",     "CONCAT(UPPER(NULL::VARCHAR), ' ', 'Smith')", None, "CONCAT()"),
  ("clickhouse", "concat(upper(CAST(NULL AS Nullable(String))), ' ', 'Smith')", None, "concat()"),
  ("arangodb",   'CONCAT(UPPER(null), " ", "Smith")', None, "CONCAT()"),
  ("r2g",        'CONCAT(UPPER(@first), " ", @last)', {"first": None, "last": "Smith"}, "evaluator"),
  ("python",     "first.upper() + ' ' + last", {"first": None, "last": "Smith"}, "native"),
  ("cel",        "first.upperAscii() + ' ' + last", {"first": None, "last": "Smith"}, "strings ext"),
]
P["P2 first non-null of ('', NULL, 'unknown')"] = [
  ("postgres",   "COALESCE('', NULL, 'unknown')", None, "COALESCE"),
  ("duckdb",     "COALESCE('', NULL, 'unknown')", None, "COALESCE"),
  ("clickhouse", "coalesce('', NULL, 'unknown')", None, "coalesce"),
  ("arangodb",   'NOT_NULL("", null, "unknown")', None, "NOT_NULL()"),
  ("arangodb",   '"" ?? null ?? "unknown"', None, "?? operator"),
  ("arangodb",   '"" || "unknown"', None, "|| (truthiness)"),
  ("r2g",        '@phone ?? @mobile ?? "unknown"', {"phone": "", "mobile": None}, "?? operator"),
  ("python",     "next(x for x in (phone, mobile, 'unknown') if x is not None)", {"phone": "", "mobile": None}, "native"),
  ("cel",        "phone != null ? phone : (mobile != null ? mobile : 'unknown')", {"phone": "", "mobile": None}, "ternary"),
]
P["P3a text '00123' to number"] = [
  ("postgres",   "CAST('00123' AS INTEGER)", None, "CAST"),
  ("duckdb",     "CAST('00123' AS INTEGER)", None, "CAST"),
  ("clickhouse", "toInt64('00123')", None, "toInt64"),
  ("arangodb",   'TO_NUMBER("00123")', None, "TO_NUMBER"),
  ("r2g",        'TO_NUMBER(@x)', {"x": "00123"}, "TO_NUMBER"),
  ("python",     "int(x)", {"x": "00123"}, "int()"),
  ("cel",        "int(x)", {"x": "00123"}, "int()"),
]
P["P3b text '12a' to number"] = [
  ("postgres",   "CAST('12a' AS INTEGER)", None, "CAST"),
  ("duckdb",     "CAST('12a' AS INTEGER)", None, "CAST"),
  ("duckdb",     "TRY_CAST('12a' AS INTEGER)", None, "TRY_CAST"),
  ("clickhouse", "toInt64('12a')", None, "toInt64"),
  ("clickhouse", "toInt64OrNull('12a')", None, "toInt64OrNull"),
  ("arangodb",   'TO_NUMBER("12a")', None, "TO_NUMBER"),
  ("r2g",        'TO_NUMBER(@x)', {"x": "12a"}, "TO_NUMBER"),
  ("python",     "int(x)", {"x": "12a"}, "int()"),
  ("cel",        "int(x)", {"x": "12a"}, "int()"),
]
P["P4 risk band when score is NULL  (<30 low, <70 medium, else high)"] = [
  ("postgres",   "CASE WHEN CAST(NULL AS INTEGER) < 30 THEN 'low' WHEN CAST(NULL AS INTEGER) < 70 THEN 'medium' ELSE 'high' END", None, "CASE"),
  ("duckdb",     "CASE WHEN CAST(NULL AS INTEGER) < 30 THEN 'low' WHEN CAST(NULL AS INTEGER) < 70 THEN 'medium' ELSE 'high' END", None, "CASE"),
  ("clickhouse", "CASE WHEN CAST(NULL AS Nullable(Int32)) < 30 THEN 'low' WHEN CAST(NULL AS Nullable(Int32)) < 70 THEN 'medium' ELSE 'high' END", None, "CASE"),
  ("arangodb",   'null < 30 ? "low" : (null < 70 ? "medium" : "high")', None, "ternary"),
  ("r2g",        '@s < 30 ? "low" : (@s < 70 ? "medium" : "high")', {"s": None}, "ternary"),
  ("python",     "'low' if s < 30 else ('medium' if s < 70 else 'high')", {"s": None}, "native"),
  ("cel",        "s < 30 ? 'low' : (s < 70 ? 'medium' : 'high')", {"s": None}, "ternary"),
]
P["P5 composite key  tenant=7, id=42.0 (a float)"] = [
  ("postgres",   "CAST(7 AS TEXT) || '_' || CAST(CAST(42.0 AS DOUBLE PRECISION) AS TEXT)", None, "cast + ||"),
  ("duckdb",     "CAST(7 AS VARCHAR) || '_' || CAST(CAST(42.0 AS DOUBLE) AS VARCHAR)", None, "cast + ||"),
  ("clickhouse", "concat(toString(7), '_', toString(toFloat64(42.0)))", None, "toString"),
  ("arangodb",   'CONCAT(7, "_", 42.0)', None, "CONCAT"),
  ("r2g",        'CONCAT(@t, "_", @id)', {"t": 7, "id": 42.0}, "CONCAT"),
  ("python",     "str(t) + '_' + str(i)", {"t": 7, "i": 42.0}, "str()"),
  ("cel",        "string(t) + '_' + string(i)", {"t": 7, "i": 42.0}, "string()"),
]
P["P6 timestamp '2026-03-29 01:30:00+02:00' as text"] = [
  # Postgres and DuckDB both render TIMESTAMPTZ in the *session* time zone, so
  # record it: Postgres takes the server's TimeZone setting, DuckDB the host's.
  ("postgres",   "current_setting('TimeZone')", None, "session tz"),
  ("duckdb",     "current_setting('TimeZone')", None, "session tz"),
  ("postgres",   "CAST(CAST('2026-03-29 01:30:00+02:00' AS TIMESTAMPTZ) AS TEXT)", None, "cast to text"),
  ("duckdb",     "CAST(CAST('2026-03-29 01:30:00+02:00' AS TIMESTAMPTZ) AS VARCHAR)", None, "cast to text"),
  ("clickhouse", "toString(parseDateTimeBestEffort('2026-03-29 01:30:00+02:00'))", None, "toString"),
  ("arangodb",   'DATE_ISO8601("2026-03-29T01:30:00+02:00")', None, "DATE_ISO8601"),
  ("python",     "datetime.datetime.fromisoformat(x).astimezone(datetime.timezone.utc).isoformat()", {"x": "2026-03-29 01:30:00+02:00"}, "to UTC"),
  ("cel",        "string(timestamp('2026-03-29T01:30:00+02:00'))", None, "string(timestamp)"),
]
P["P7 first three characters of 'Zürich'"] = [
  ("postgres",   "SUBSTRING('Zürich', 1, 3)", None, "1-based"),
  ("duckdb",     "SUBSTRING('Zürich', 1, 3)", None, "1-based"),
  ("clickhouse", "substring('Zürich', 1, 3)", None, "substring"),
  ("clickhouse", "substringUTF8('Zürich', 1, 3)", None, "substringUTF8"),
  ("arangodb",   'SUBSTRING("Zürich", 0, 3)', None, "0-based"),
  ("arangodb",   'SUBSTRING("Zürich", 1, 3)', None, "SQL-style args"),
  ("r2g",        'SUBSTRING(@s, 0, 3)', {"s": "Zürich"}, "0-based"),
  ("python",     "s[0:3]", {"s": "Zürich"}, "slice"),
  ("cel",        "s.substring(0, 3)", {"s": "Zürich"}, "strings ext"),
]
P["P8 seven divided by two"] = [
  ("postgres", "7 / 2", None, ""), ("duckdb", "7 / 2", None, ""), ("clickhouse", "7 / 2", None, ""),
  ("arangodb", "7 / 2", None, ""), ("r2g", "7 / 2", None, ""), ("python", "7 / 2", None, ""),
  ("cel", "7 / 2", None, ""),
]

out = {}
for probe, cases in P.items():
    print(f"\n{probe}")
    out[probe] = []
    for engine, expr, env, form in cases:
        r = run(engine, expr, env)
        shown = repr(r["value"]) if r["ok"] else "ERROR  " + r["error"]
        print(f"  {engine:<11} {form:<16} {shown}")
        out[probe].append({"engine": engine, "form": form, "expr": expr, **r})
json.dump(out, open(sys.argv[1], "w"), indent=1, default=str)
