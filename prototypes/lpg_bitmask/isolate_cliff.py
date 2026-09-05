"""Which phase actually exhausts memory?

The soak died at 10M edges with the container OOM-killed, but it built both
indexes in one run, so the kill could not be attributed. Here each phase is
separated and the container is checked between them, because the answer decides
the argument: if the ARRAY build is what dies while the BITMASK build survives on
identical data, that is the sizing case. If the load itself dies, it is a
hardware statement about the harness and says nothing about the representation.
"""
from __future__ import annotations
import json, random, subprocess, sys, time
from pathlib import Path
from arango import ArangoClient
sys.path.insert(0, str(Path(__file__).parent))
from dictionary import LabelDictionary

ENDPOINT, PW = "http://localhost:8540", "r2g_test_2026"
EDGE_VOCAB = [f"EL{i:02d}" for i in range(25)]
NODE_VOCAB = [f"NL{i:02d}" for i in range(30)]
LPE = LPN = 20
N_EDGES, N_NODES = 10_000_000, 2_000_000
BATCH = 50_000


def mem():
    try:
        o = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}",
                            "r2g-test-arangodb"], capture_output=True, text=True, timeout=20)
        return o.stdout.strip().split("/")[0].strip() or "?"
    except Exception:
        return "?"


def alive():
    o = subprocess.run(["docker", "inspect", "r2g-test-arangodb", "--format",
                        "{{.State.Status}}|{{.State.OOMKilled}}"],
                       capture_output=True, text=True)
    st, oom = o.stdout.strip().split("|")
    return st == "running", oom == "true"


def phase(tag, fn):
    t0 = time.time()
    try:
        fn()
        ok, oom = alive()
        print(f"  {tag:34s} {time.time()-t0:7.1f}s  mem={mem():>10}  "
              f"{'OK' if ok else 'CONTAINER DEAD'}{' (OOM)' if oom else ''}", flush=True)
        return ok
    except Exception as e:
        ok, oom = alive()
        print(f"  {tag:34s} {time.time()-t0:7.1f}s  FAILED: {type(e).__name__}  "
              f"container={'up' if ok else 'DEAD'}{' OOM-KILLED' if oom else ''}", flush=True)
        return False


def main():
    ed = LabelDictionary("e"); ed.extend(EDGE_VOCAB)
    nd = LabelDictionary("n"); nd.extend(NODE_VOCAB)
    rng = random.Random(4)
    client = ArangoClient(hosts=ENDPOINT, request_timeout=7200)
    sysdb = client.db("_system", username="root", password=PW)
    name = "cliff"
    sysdb.delete_database(name, ignore_missing=True); sysdb.create_database(name)
    db = client.db(name, username="root", password=PW)
    nodes = db.create_collection("nodes"); edges = db.create_collection("edges", edge=True)
    print(f"baseline mem={mem()}   target {N_EDGES:,} edges x {LPE} labels "
          f"= {N_EDGES*LPE/1e6:.0f}M array entries\n", flush=True)

    def load_nodes():
        buf = []
        for i in range(N_NODES):
            labs = rng.sample(NODE_VOCAB, LPN)
            buf.append({"_key": f"N{i}", "labels": labs, "labelBits": nd.mask(labs)})
            if len(buf) >= BATCH: nodes.insert_many(buf); buf.clear()
        if buf: nodes.insert_many(buf)

    def load_edges():
        buf = []
        for _ in range(N_EDGES):
            t = rng.sample(EDGE_VOCAB, LPE)
            buf.append({"_from": f"nodes/N{rng.randrange(N_NODES)}",
                        "_to": f"nodes/N{rng.randrange(N_NODES)}",
                        "types": t, "typeBits": ed.mask(t)})
            if len(buf) >= BATCH: edges.insert_many(buf); buf.clear()
        if buf: edges.insert_many(buf)

    if not phase(f"load {N_NODES:,} nodes", load_nodes): return
    if not phase(f"load {N_EDGES:,} edges", load_edges): return
    if not phase("build BITMASK index (10M entries)",
                 lambda: edges.add_index({"type": "persistent",
                                          "fields": ["_from", "typeBits"], "name": "bits"})):
        return
    phase(f"build ARRAY index ({N_EDGES*LPE/1e6:.0f}M entries)",
          lambda: edges.add_index({"type": "persistent",
                                   "fields": ["_from", "types[*]"], "name": "arr"}))
    try:
        sysdb.delete_database(name, ignore_missing=True)
    except Exception:
        pass


if __name__ == "__main__":
    main()
