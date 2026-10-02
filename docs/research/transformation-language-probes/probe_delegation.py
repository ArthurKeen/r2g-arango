"""Section 5 — r2g's local evaluator versus its own AQL delegation, same row.

The server side uses r2g's real query builder (``NodeTransformer.build_delegation_query``)
and sends the row as the ``@rows`` bind variable, exactly as
``StreamingPipeline._apply_delegation`` does. The "route" column records which
side r2g would actually use for the expression as written: an expression that
compiles locally is evaluated locally on every load path; one that does not is
delegated on the streaming path and has no bulk-load path at all (P5c.1.6).
"""
import json, sys, os
from types import SimpleNamespace
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "src"))
from arango import ArangoClient
from r2g.expressions import ExpressionError, compile_expression
from r2g.transformers.node_transformer import NodeTransformer
from r2g.types import Column, CollectionMapping, FieldExpression, Table
from connections import ARANGO_ENDPOINT, ARANGO_USER, ARANGO_PASSWORD, ARANGO_DB

adb = ArangoClient(hosts=ARANGO_ENDPOINT).db(ARANGO_DB, username=ARANGO_USER, password=ARANGO_PASSWORD)
print("ArangoDB", adb.version())


def server(expr, row):
    # build_delegation_query reads only ``self._delegated``; call it on a stand-in
    # so an expression the local evaluator *can* compile is still rendered the
    # way delegation would render it.
    q = NodeTransformer.build_delegation_query(
        SimpleNamespace(_delegated=[FieldExpression(target="v", expression=expr)]))
    return next(adb.aql.execute(q, bind_vars={"rows": [row]}))["v"]


def local(expr, row):
    return compile_expression(expr).evaluate(row)


def route(expr):
    try:
        compile_expression(expr)
        return "local"
    except ExpressionError:
        return "server"


def run(fn, expr, row):
    try:
        return {"ok": True, "value": fn(expr, row)}
    except Exception as ex:
        return {"ok": False, "error": type(ex).__name__ + ": " + str(ex).strip().splitlines()[0][:110]}


CASES = [
    ("TO_NUMBER(@x)",                                   {"x": "00123"}),
    ("TO_NUMBER(@x)",                                   {"x": "12a"}),
    ('@s < 30 ? "low" : (@s < 70 ? "medium" : "high")', {"s": None}),
    ('CONCAT(@t, "_", @id)',                            {"t": 7, "id": 42.0}),
    ('UPPER(@first) + " " + @last',                     {"first": "Ada", "last": "Lovelace"}),
    ('@n + "1"',                                        {"n": 1}),
    ("@x + 1",                                          {"x": None}),
    ("@x * 2",                                          {"x": None}),
    ("@x / 0",                                          {"x": 1}),
    ("COALESCE(@a, @b)",                                {"a": None, "b": "z"}),
    ("@a ?? @b",                                        {"a": None, "b": "z"}),
    # Outside the subset (SUBSTITUTE), so this one really is delegated — and the
    # CONCAT inside it now follows the server's rules, not the evaluator's.
    ('SUBSTITUTE(CONCAT(@t, "_", @id), " ", "")',       {"t": 7, "id": 42.0}),
]

out = []
for expr, row in CASES:
    lo, sv = run(local, expr, row), run(server, expr, row)
    agree = lo == sv or (not lo["ok"] and not sv["ok"])
    shown = lambda r: repr(r["value"]) if r["ok"] else "ERROR " + r["error"]
    print(f"\n{expr}   row={row}   route={route(expr)}   {'same' if agree else 'DIFFERENT'}"
          f"\n  local   {shown(lo)}\n  server  {shown(sv)}")
    out.append({"expr": expr, "row": row, "route": route(expr), "local": lo, "server": sv, "agree": agree})

# What a bulk load does with a delegated expression: transform_row leaves the
# target unset for the server to fill, and the bulk path never calls the server.
table = Table(name="t", primary_key=["id"], foreign_keys=[], columns=[
    Column(name="id", data_type="integer", is_nullable=False, is_primary_key=True),
    Column(name="name", data_type="text", is_nullable=True)])
mapping = CollectionMapping(source_table="t", target_collection="t", field_expressions=[
    FieldExpression(target="slug", sources=["name"], expression='SUBSTITUTE(@name, " ", "-")')])
bulk_doc = NodeTransformer(table, mapping).transform_row({"id": 1, "name": "Ada Lovelace"})
print(f"\nbulk transform_row with delegated target 'slug': {bulk_doc}")

json.dump({"cases": out, "bulk_doc_with_delegated_target": bulk_doc},
          open(sys.argv[1], "w"), indent=1, default=str)
