"""Scaling ladder: does the bitmask VCI hold its shape as the graph grows?

Streams generation so Python does not exhaust memory before ArangoDB does, and
drops each database after measuring so the ladder does not accumulate.

Measures at each rung: load throughput, post-compaction bytes, and QUERY LATENCY
for the bitmask form against the array form. Latency is the point — storage and
load rate scale predictably, but a vertex-centric index is only worth having if
traversal stays flat as the graph grows. A query whose cost tracks graph size is
not using the index no matter what the plan says.
"""
from __future__ import annotations
import random, statistics, sys, time
from pathlib import Path
import requests
from arango import ArangoClient
sys.path.insert(0, str(Path(__file__).parent))
from dictionary import LabelDictionary

ENDPOINT, PW = "http://localhost:8540", "r2g_test_2026"
NODE_LABELS = ["User"] + [f"Trait{i:02d}" for i in range(29)]
EDGE_LABELS = ["Follows", "Blocks", "Mentions", "Reposts", "Replies"]
LPN, LPE = 9, 3
BATCH = 50_000


def dicts():
    nd = LabelDictionary("node_labels"); nd.extend(NODE_LABELS)
    ed = LabelDictionary("edge_labels"); ed.extend(EDGE_LABELS)
    return nd, ed


def stream_nodes(n, nd, rng):
    for i in range(n):
        labs = ["User"] + rng.sample(NODE_LABELS[1:], LPN - 1)
        yield {"_key": f"User_{i}", "labels": labs, "labelBits": nd.mask(labs),
               "handle": f"user{i}", "followers": rng.randrange(60000)}


def stream_edges(n, nn, ed, nd, rng):
    for _ in range(n):
        a, b = rng.randrange(nn), rng.randrange(nn)
        t = rng.sample(EDGE_LABELS, LPE)
        # Endpoint masks: recomputed from the same seedless rule rather than
        # held in a dict, so the ladder is not bounded by an in-memory join.
        yield {"_from": f"nodes/User_{a}", "_to": f"nodes/User_{b}",
               "types": t, "typeBits": ed.mask(t),
               "fromLabelBits": rng.randrange(1 << 20),
               "toLabelBits": rng.randrange(1 << 20)}


def insert_stream(coll, gen):
    buf, total = [], 0
    for doc in gen:
        buf.append(doc)
        if len(buf) >= BATCH:
            coll.insert_many(buf, overwrite=False); total += len(buf); buf = []
    if buf:
        coll.insert_many(buf, overwrite=False); total += len(buf)
    return total


def timed(db, q, bv, runs=7):
    db.aql.execute(q, bind_vars=bv)          # warm
    ts = []
    for _ in range(runs):
        t0 = time.perf_counter(); list(db.aql.execute(q, bind_vars=bv))
        ts.append((time.perf_counter() - t0) * 1000)
    return statistics.median(ts)


def rung(n_nodes, n_edges):
    nd, ed = dicts(); rng = random.Random(5)
    client = ArangoClient(hosts=ENDPOINT)
    sysdb = client.db("_system", username="root", password=PW)
    name = f"scale_{n_nodes}"
    sysdb.delete_database(name, ignore_missing=True)
    sysdb.create_database(name)
    db = client.db(name, username="root", password=PW)
    try:
        nodes = db.create_collection("nodes"); edges = db.create_collection("edges", edge=True)
        edges.add_index({"type": "persistent", "fields": ["_from", "typeBits"], "name": "vci_bits"})
        edges.add_index({"type": "persistent", "fields": ["_from", "types[*]"], "name": "vci_arr"})
        nodes.add_index({"type": "persistent", "fields": ["labels[*]"], "name": "n_arr"})
        nodes.add_index({"type": "persistent", "fields": ["labelBits"], "name": "n_bits"})
        t0 = time.time()
        insert_stream(nodes, stream_nodes(n_nodes, nd, rng))
        insert_stream(edges, stream_edges(n_edges, n_nodes, ed, nd, rng))
        load = time.time() - t0

        start = list(db.aql.execute("FOR e IN edges COLLECT f=e._from WITH COUNT INTO n "
                                    "SORT n DESC LIMIT 1 RETURN f"))[0]
        masks = ed.masks_containing("Follows")
        HB = "OPTIONS {indexHint:{edges:{outbound:{base:['vci_bits']}}}}"
        HA = "OPTIONS {indexHint:{edges:{outbound:{base:['vci_arr']}}}}"
        qb = (f"WITH nodes\nFOR v,e IN 1..1 OUTBOUND @s edges {HB} "
              f"FILTER e.typeBits IN @m RETURN v._key")
        qa = (f"WITH nodes\nFOR v,e IN 1..1 OUTBOUND @s edges {HA} "
              f"FILTER @l IN e.types RETURN v._key")
        tb = timed(db, qb, {"s": start, "m": masks})
        ta = timed(db, qa, {"s": start, "l": "Follows"})
        # entry-point lookup: all nodes with one label
        qe = "FOR n IN nodes FILTER @l IN n.labels LIMIT 100 RETURN n._key"
        te = timed(db, qe, {"l": "Trait07"})

        for c in ("nodes", "edges"):
            requests.put(f"{ENDPOINT}/_db/{name}/_api/collection/{c}/compact",
                         auth=("root", PW), timeout=600)
        time.sleep(20)
        fn, fe = nodes.statistics(), edges.statistics()
        bn = (fn["documents_size"] + fn["indexes"]["size"]) / n_nodes
        be = (fe["documents_size"] + fe["indexes"]["size"]) / n_edges
        print(f"{n_nodes:>9,} {n_edges:>10,} {load:>7.0f}s {(n_nodes+n_edges)/load:>9,.0f} "
              f"{bn:>7.0f} {be:>7.0f} {tb:>9.2f} {ta:>9.2f} {te:>9.2f}", flush=True)
    finally:
        sysdb.delete_database(name, ignore_missing=True); client.close()


if __name__ == "__main__":
    print(f"{'nodes':>9} {'edges':>10} {'load':>8} {'docs/s':>9} "
          f"{'B/node':>7} {'B/edge':>7} {'bitmask':>9} {'array':>9} {'entry':>9}", flush=True)
    print(f"{'':>9} {'':>10} {'':>8} {'':>9} {'':>7} {'':>7} {'(ms)':>9} {'(ms)':>9} {'(ms)':>9}", flush=True)
    for nn, ne in [(250_000, 750_000), (1_000_000, 3_000_000), (3_000_000, 9_000_000)]:
        rung(nn, ne)
