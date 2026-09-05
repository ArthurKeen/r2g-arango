"""Latency at CONTROLLED degree.

The ladder picked the highest-degree node as its start, and max degree grows
with the graph — so its latency curve conflates "index got slower" with "the
query returned more rows". Here every scale uses a start node with the SAME
out-degree, so graph size is the only variable. If a vertex-centric index is
doing its job the line should be flat: a point lookup on `_from` should not care
how many edges exist elsewhere.
"""
from __future__ import annotations
import random, statistics, sys, time
from pathlib import Path
from arango import ArangoClient
sys.path.insert(0, str(Path(__file__).parent))
from dictionary import LabelDictionary

ENDPOINT, PW = "http://localhost:8540", "r2g_test_2026"
NODE_LABELS = ["User"] + [f"Trait{i:02d}" for i in range(29)]
EDGE_LABELS = ["Follows", "Blocks", "Mentions", "Reposts", "Replies"]
DEGREE = 40          # every probe node has exactly this many out-edges
BATCH = 50_000


def timed(db, q, bv, runs=9):
    db.aql.execute(q, bind_vars=bv)
    ts = []
    for _ in range(runs):
        t0 = time.perf_counter(); rows = list(db.aql.execute(q, bind_vars=bv))
        ts.append((time.perf_counter() - t0) * 1000)
    return statistics.median(ts), len(rows)


def rung(n_nodes, n_edges):
    ed = LabelDictionary("edge_labels"); ed.extend(EDGE_LABELS)
    nd = LabelDictionary("node_labels"); nd.extend(NODE_LABELS)
    rng = random.Random(5)
    client = ArangoClient(hosts=ENDPOINT)
    sysdb = client.db("_system", username="root", password=PW)
    name = f"fd_{n_nodes}"
    sysdb.delete_database(name, ignore_missing=True); sysdb.create_database(name)
    db = client.db(name, username="root", password=PW)
    try:
        nodes = db.create_collection("nodes"); edges = db.create_collection("edges", edge=True)
        edges.add_index({"type": "persistent", "fields": ["_from", "typeBits"], "name": "vci_bits"})
        edges.add_index({"type": "persistent", "fields": ["_from", "types[*]"], "name": "vci_arr"})
        buf = []
        for i in range(n_nodes):
            labs = ["User"] + rng.sample(NODE_LABELS[1:], 8)
            buf.append({"_key": f"User_{i}", "labels": labs, "labelBits": nd.mask(labs)})
            if len(buf) >= BATCH: nodes.insert_many(buf); buf = []
        if buf: nodes.insert_many(buf)

        # PROBE node 0 gets exactly DEGREE out-edges; the rest are spread randomly.
        buf = []
        for j in range(DEGREE):
            t = rng.sample(EDGE_LABELS, 3)
            buf.append({"_from": "nodes/User_0", "_to": f"nodes/User_{1+j}",
                        "types": t, "typeBits": ed.mask(t)})
        remaining = n_edges - DEGREE
        for _ in range(remaining):
            a = rng.randrange(1, n_nodes); b = rng.randrange(n_nodes)
            t = rng.sample(EDGE_LABELS, 3)
            buf.append({"_from": f"nodes/User_{a}", "_to": f"nodes/User_{b}",
                        "types": t, "typeBits": ed.mask(t)})
            if len(buf) >= BATCH: edges.insert_many(buf); buf = []
        if buf: edges.insert_many(buf)

        masks = ed.masks_containing("Follows")
        HB = "OPTIONS {indexHint:{edges:{outbound:{base:['vci_bits']}}}}"
        HA = "OPTIONS {indexHint:{edges:{outbound:{base:['vci_arr']}}}}"
        s = "nodes/User_0"
        tb, rb = timed(db, f"WITH nodes\nFOR v,e IN 1..1 OUTBOUND @s edges {HB} "
                           f"FILTER e.typeBits IN @m RETURN v._key", {"s": s, "m": masks})
        ta, ra = timed(db, f"WITH nodes\nFOR v,e IN 1..1 OUTBOUND @s edges {HA} "
                           f"FILTER @l IN e.types RETURN v._key", {"s": s, "l": "Follows"})
        print(f"{n_nodes:>9,} {n_edges:>10,} {tb:>10.2f} {rb:>7} {ta:>10.2f} {ra:>7} "
              f"{ra/max(rb,1):>7.1f}x", flush=True)
    finally:
        sysdb.delete_database(name, ignore_missing=True); client.close()


if __name__ == "__main__":
    print(f"start node has EXACTLY {DEGREE} out-edges at every scale\n")
    print(f"{'nodes':>9} {'edges':>10} {'bitmask':>10} {'rows':>7} {'array':>10} {'rows':>7} {'inflate':>8}")
    print(f"{'':>9} {'':>10} {'(ms)':>10} {'':>7} {'(ms)':>10} {'':>7} {'':>8}")
    for nn, ne in [(200_000, 600_000), (800_000, 2_400_000), (2_400_000, 7_200_000)]:
        rung(nn, ne)
