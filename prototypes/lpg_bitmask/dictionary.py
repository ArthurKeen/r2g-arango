"""Bidirectional label dictionary — label name <-> bit position.

Standalone prototype. Imports nothing from r2g.

Two invariants make this a deployment artifact rather than a runtime detail:

* **Bit assignments are permanent.** A stored mask is meaningless without the
  dictionary that produced it, so reassigning a bit silently reinterprets every
  row ever written. Adding a label is safe; renumbering is a full rewrite. The
  class refuses to renumber rather than trusting callers to remember.
* **Both sides need the same version.** ETL encodes with it and query generation
  compiles label names into masks with it. A query built under v2 run against
  data written under v1 returns wrong rows with no error, so the version is
  stamped and checked rather than assumed.

Vocabularies larger than 32 labels are *chunked* across several scalar fields
rather than rejected — see `BITS_PER_CHUNK`.
"""

from __future__ import annotations

import json
from pathlib import Path


class LabelDictionaryError(RuntimeError):
    pass


class LabelDictionary:
    #: AQL's BIT_* functions are 32-bit and fail SILENTLY past it:
    #: BIT_AND(2**32, 2**32) returns null. This is a limit on *arithmetic*, not
    #: on storage — a document round-trips an exact integer to 2**63 — so it
    #: bounds one chunk, not the vocabulary. Beyond 32 labels the mask is split
    #: across `bits0, bits1, ...` scalar fields (see `chunk_fields`).
    BITS_PER_CHUNK = 32

    #: Guard on index width, not an engine limit. Each chunk adds a field to the
    #: vertex-centric index and one BIT_AND to every traversal filter, so an
    #: unbounded vocabulary should be a deliberate decision, not a typo.
    DEFAULT_MAX_CHUNKS = 8

    def __init__(self, name: str, version: int = 1,
                 max_chunks: int = DEFAULT_MAX_CHUNKS) -> None:
        self.name = name
        self.version = version
        self.max_chunks = max_chunks
        self._to_bit: dict[str, int] = {}
        self._to_label: dict[int, str] = {}

    @property
    def max_bits(self) -> int:
        return self.BITS_PER_CHUNK * self.max_chunks

    @property
    def n_chunks(self) -> int:
        """How many 32-bit fields this vocabulary currently needs (min 1)."""
        return max(1, -(-len(self._to_bit) // self.BITS_PER_CHUNK))

    # -- construction -------------------------------------------------

    def add(self, label: str) -> int:
        """Assign the next free bit. Idempotent for an existing label."""
        if label in self._to_bit:
            return self._to_bit[label]
        pos = len(self._to_bit)
        if pos >= self.max_bits:
            raise LabelDictionaryError(
                f"vocabulary exceeds {self.max_bits} labels ({label!r} would be "
                f"bit {pos}); raise max_chunks to widen the index, or split the "
                f"vocabulary — each chunk costs an index field and a BIT_AND"
            )
        self._to_bit[label] = pos
        self._to_label[pos] = label
        return pos

    def extend(self, labels) -> None:
        for lab in labels:
            self.add(lab)

    # -- encode / decode ----------------------------------------------

    def _bit_of(self, label: str) -> int:
        if label not in self._to_bit:
            raise LabelDictionaryError(
                f"{label!r} is not in dictionary {self.name!r} v{self.version}; "
                f"encoding it now would assign a bit the stored data does not use"
            )
        return self._to_bit[label]

    def mask(self, labels) -> int:
        """Label set -> a single integer mask.

        Only valid while the vocabulary fits one chunk. Past that a single
        integer cannot be BIT_AND-ed by AQL at all, so this raises rather than
        returning a value that would silently evaluate to null server-side.
        """
        if len(self._to_bit) > self.BITS_PER_CHUNK:
            raise LabelDictionaryError(
                f"{len(self)} labels needs {self.n_chunks} chunks; use chunks() "
                f"— a single mask past bit 31 makes AQL's BIT_AND return null"
            )
        m = 0
        for lab in labels:
            m |= 1 << self._bit_of(lab)
        return m

    def chunks(self, labels) -> list[int]:
        """Label set -> one 32-bit integer per chunk.

        The general form of `mask()`. Each element stays inside BIT_*'s 32-bit
        range and is stored in its own scalar field, so the vertex-centric index
        never sees an array and never duplicates entries.
        """
        out = [0] * self.n_chunks
        for lab in labels:
            b = self._bit_of(lab)
            out[b // self.BITS_PER_CHUNK] |= 1 << (b % self.BITS_PER_CHUNK)
        return out

    def labels(self, mask) -> list[str]:
        """Mask (int) or chunk list -> label set (the reverse direction)."""
        chunks = [mask] if isinstance(mask, int) else list(mask)
        out = []
        for ci, cv in enumerate(chunks):
            base = ci * self.BITS_PER_CHUNK
            for b in range(self.BITS_PER_CHUNK):
                if cv & (1 << b) and (base + b) in self._to_label:
                    out.append(self._to_label[base + b])
        return out

    def bit(self, label: str) -> int:
        """The single-chunk bit value for `label` (one-chunk vocabularies)."""
        return 1 << self._bit_of(label)

    # -- query compilation --------------------------------------------

    def chunk_fields(self, prefix: str = "bits") -> list[str]:
        """Document/index field names holding the mask: bits0, bits1, ..."""
        return [f"{prefix}{i}" for i in range(self.n_chunks)]

    def chunk_filter(self, labels, *, var: str = "e", prefix: str = "bits",
                     bind: str = "m") -> tuple[str, dict[str, int]]:
        """AQL subset test ("has all of `labels`") plus its bind vars.

        Emits one BIT_AND per chunk. Chunks whose mask is 0 are omitted: they
        constrain nothing, and skipping them keeps the common case (a target
        label living in one chunk) down to a single comparison.
        """
        want = self.chunks(labels)
        parts, bv = [], {}
        for i, m in enumerate(want):
            if not m:
                continue
            key = f"{bind}{i}"
            parts.append(f"BIT_AND({var}.{prefix}{i}, @{key}) == @{key}")
            bv[key] = m
        return (" AND ".join(parts) or "true"), bv

    def masks_containing(self, label: str) -> list[int]:
        """Every mask value in this vocabulary that includes ``label``.

        This is what makes a subset test index-servable: `bits IN [...]` is an
        equality set on an indexed scalar, unlike BIT_AND which is a function of
        the column and can only post-filter. Enumeration is 2**(n-1) values, so
        it is practical for a small vocabulary and impossible for a large one —
        `enumerable()` is the check a query planner should make. Chunked
        vocabularies are never enumerable.
        """
        if not self.enumerable():
            raise LabelDictionaryError(
                f"vocabulary of {len(self)} labels would enumerate to "
                f"{2 ** max(len(self) - 1, 0)} masks; use chunk_filter() "
                f"(post-filter) instead"
            )
        bit = self.bit(label)
        return [m for m in range(1 << len(self)) if m & bit]

    def enumerable(self, budget: int = 4096) -> bool:
        """Whether `masks_containing` stays within a sane bind-parameter size."""
        if len(self) == 0 or len(self) > self.BITS_PER_CHUNK:
            return False
        return (1 << (len(self) - 1)) <= budget

    # -- persistence ---------------------------------------------------

    def save(self, path) -> None:
        Path(path).write_text(json.dumps({
            "name": self.name, "version": self.version,
            "bits_per_chunk": self.BITS_PER_CHUNK,
            "max_chunks": self.max_chunks,
            "bits": self._to_bit,
        }, indent=2, sort_keys=True), encoding="utf-8")

    @classmethod
    def load(cls, path) -> "LabelDictionary":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        stored = d.get("bits_per_chunk", cls.BITS_PER_CHUNK)
        if stored != cls.BITS_PER_CHUNK:
            # Chunk width decides which bit lands in which field, so a mismatch
            # reinterprets every stored mask. Refuse rather than mis-decode.
            raise LabelDictionaryError(
                f"dictionary was written with {stored} bits per chunk, this "
                f"build uses {cls.BITS_PER_CHUNK} — stored masks would mis-decode"
            )
        obj = cls(d["name"], d["version"], d.get("max_chunks", cls.DEFAULT_MAX_CHUNKS))
        for lab, pos in sorted(d["bits"].items(), key=lambda kv: kv[1]):
            if pos != len(obj._to_bit):
                raise LabelDictionaryError(
                    f"non-contiguous bit assignment for {lab!r} (expected "
                    f"{len(obj._to_bit)}, found {pos}) — dictionary is corrupt"
                )
            obj.add(lab)
        return obj

    def __len__(self) -> int:
        return len(self._to_bit)

    def __repr__(self) -> str:
        return (f"<LabelDictionary {self.name!r} v{self.version} "
                f"{len(self)} labels / {self.n_chunks} chunk(s)>")
