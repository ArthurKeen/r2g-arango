# LPG label bitmasks — prototype

Explores whether a **bitmask** representation of LPG labels solves three problems
at once for a large multi-label graph:

1. **The multi-label VCI.** An array-valued label field cannot go in a
   vertex-centric index — a persistent index stores one entry per array element
   and a traversal scans them all, so each edge is returned once per label. A
   bitmask is a **scalar**, so it indexes without expansion.
2. **Memory.** A label set becomes one integer instead of N strings, on both the
   node and the endpoint copies denormalized onto each edge.
3. **Pushdown.** Scalars restore the filter forms that a list field breaks.

Compression is not a separate optimisation here — it is what makes (1) work.

## Files

| | |
| --- | --- |
| `dictionary.py` | Bidirectional label ↔ bit-position map, persisted and versioned |
| `etl.py` | Row-level labels from categorical columns + the endpoint-mask join |
| `harness.py` | Loads array and bitmask representations side by side and compares |
| `scale.py` | Scaling ladder: throughput, storage, latency |
| `fixed_degree.py` | Latency at controlled start-degree (removes a confound in `scale.py`) |
| `soak.py` | Long run hunting the index-build cliff |

## Two constraints the prototype enforces rather than documents

- **32-bit ceiling.** AQL's `BIT_*` functions are 32-bit; `BIT_AND(2**32, 2**32)`
  returns `null` *silently*. `LabelDictionary` raises instead. This caught a real
  error while building the harness: node labels (30) and edge labels (5) had been
  put in one dictionary — 35 labels, over the ceiling. They need **separate**
  dictionaries, which is fine since they are independent namespaces.
- **Bit assignments are permanent.** A stored mask is meaningless without the
  dictionary that produced it, so renumbering silently reinterprets every row
  ever written. Adding a label is safe; renumbering is a rewrite.

## Findings (measured, ArangoDB 3.12.9)

- Bitmask and array select **identical row sets**; the array form additionally
  inflates by exactly the labels-per-edge factor (3.0x at 3 labels, stable across
  every scale tested).
- Latency is **flat** as the graph grows: 1.17 → 1.41 ms across 12x data at a
  fixed start-degree. Load throughput is flat too (~23k docs/s, −2% over 12x).
- Storage per document could **not** be measured reliably — equivalent data gave
  a 90% spread across runs because compaction is asynchronous. Any sizing needs
  a real benchmark on target hardware.

## Vocabulary size decides the query strategy

| Distinct labels | Masks | Subset test |
| --- | --- | --- |
| ≤ ~13 | ≤ 4096 | `bits IN [...]` — **index-served** |
| larger (≤32) | too many to enumerate | `BIT_AND(...)` — correct, **post-filter** |
| > 32 | — | needs chunking; loses the single-scalar index |

`LabelDictionary.enumerable()` is the check a query planner should make.

## Running

Requires a live ArangoDB. Defaults to `http://localhost:8540`.

```bash
python harness.py --nodes 100000 --edges 300000
python fixed_degree.py
python soak.py
```
